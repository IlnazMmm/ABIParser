import json
import docker, json
import os, subprocess, json
from pathlib import Path
import hashlib
from eth_abi import encode as abi_encode
import tempfile
import shutil


def keccak256(data: str) -> str:
    """Keccak256 в hex-строке"""
    return "0x" + hashlib.sha3_256(data.encode()).hexdigest()

class ABIType:
    """
    Представляет тип параметра ABI (включая tuple/tuple[] и вложенные структуры).
    """
    def __init__(self, type_str: str, components: list = None):
        self.raw = type_str  # строка типа из ABI: "uint256", "tuple", "tuple[]"
        self.components = [
            ABIInput(c.get("name", ""), c["type"], c.get("indexed", False), c.get("components"))
            for c in (components or [])
        ]

    def __repr__(self):
        if self.is_tuple:
            return f"({', '.join(map(str, self.components))})"
        return self.raw

    @property
    def is_tuple(self) -> bool:
        return self.raw.startswith("tuple")

    @property
    def canonical_type(self) -> str:
        """
        Возвращает каноническое строковое представление для сигнатуры.
        tuple → (типы компонентов)
        tuple[] → (типы компонентов)[]
        """
        if self.is_tuple:
            inner = ",".join(c.type.canonical_type for c in self.components)
            suffix = self.raw[5:]  # [] если это tuple[]
            return f"({inner}){suffix}"
        return self.raw

class ABIInput:
    """
    Описывает входной/выходной параметр ABI (тип, имя, признак indexed для событий).
    """
    def __init__(self, name, type_: str, indexed=False, components=None):
        self.name = name
        self.type = ABIType(type_, components)
        self.indexed = indexed

    def __repr__(self):
        return f"{self.type.canonical_type} {self.name}".strip()


class ABIEntry:
    """
    Описывает один элемент ABI (function, event, constructor, fallback, receive, error).
    Содержит параметры, сигнатуру, селектор и topic-хэш.
    """
    def __init__(self, name, inputs, outputs, state_mutability, entry_type):
        self.name = name
        self.inputs = inputs
        self.outputs = outputs
        self.state_mutability = state_mutability
        self.entry_type = entry_type  # function, event, constructor, fallback, receive, error

    @property
    def signature(self):
        """Формат Имя(типы)"""
        types = ",".join(inp.type.canonical_type for inp in self.inputs)
        if self.entry_type in ("function", "event", "error"):
            return f"{self.name}({types})"
        return None

    @property
    def selector(self):
        """Селектор функции или ошибки"""
        if self.entry_type in ("function", "error"):
            return keccak256(self.signature)[:10]  # первые 4 байта
        return None

    @property
    def topic_hash(self):
        """Topic hash для события"""
        if self.entry_type == "event":
            return keccak256(self.signature)
        return None

    def encode_abi(self, args: list):
        """
        Кодирование payload для транзакции:
        selector (4 байта) + encoded args
        args: список значений в порядке ABI
        """
        if self.entry_type != "function":
            raise ValueError("encode_abi() доступен только для функций")

        types = [inp.type for inp in self.inputs]
        encoded_args = abi_encode(types, args)
        return self.selector + encoded_args.hex()

    def __repr__(self):
        parts = [self.entry_type]
        if self.name:
            parts.append(self.name)
        if self.selector:
            parts.append(f"selector={self.selector}")
        if self.topic_hash:
            parts.append(f"topic={self.topic_hash}")
        return f"<{' '.join(parts)}>"


class Contract:
    """
    Представляет скомпилированный контракт:
    - функции
    - события
    - конструкторы
    - fallback/receive
    - ошибки
    Доступ к функциям/событиям возможен как через методы (get_function),
    так и через атрибуты (contract.transfer).
    """
    def __init__(self, name, abi):
        self.name = name
        self.entries = []      # все элементы ABI
        self.functions = {}    # имя → ABIEntry
        self.events = {}
        self.constructors = []
        self.fallbacks = []
        self.receives = []
        self.errors = {}
        self._parse_abi(abi)
        self.abi = json.dumps(abi)

    def _parse_abi(self, abi):
        for entry in abi:
            entry_type = entry.get("type")
            name = entry.get("name", "")
            inputs = [
                ABIInput(i.get("name", ""), i["type"], i.get("indexed", False), i.get("components"))
                for i in entry.get("inputs", [])
            ]
            outputs = [
                ABIInput(o.get("name", ""), o["type"], False, o.get("components"))
                for o in entry.get("outputs", [])
            ] if "outputs" in entry else []
            state_mutability = entry.get("stateMutability", "")

            abi_entry = ABIEntry(name, inputs, outputs, state_mutability, entry_type)
            self.entries.append(abi_entry)

            if entry_type == "function":
                self.functions[name] = abi_entry
            elif entry_type == "event":
                self.events[name] = abi_entry
            elif entry_type == "constructor":
                self.constructors.append(abi_entry)
            elif entry_type == "fallback":
                self.fallbacks.append(abi_entry)
            elif entry_type == "receive":
                self.receives.append(abi_entry)
            elif entry_type == "error":
                self.errors[name] = abi_entry

    def get_function(self, name):
        return self.functions.get(name)

    def get_event(self, name):
        return self.events.get(name)

    def get_error(self, name):
        return self.errors.get(name)

    def __getattr__(self, name):
        """Доступ к функциям/событиям/ошибкам через точку"""
        if name in self.functions:
            return self.functions[name]
        if name in self.events:
            return self.events[name]
        if name in self.errors:
            return self.errors[name]
        raise AttributeError(f"'{self.name}' contract has no member '{name}'")

    def __repr__(self):
        return f"<Contract {self.name}: {len(self.functions)} funcs, {len(self.events)} events, {len(self.errors)} errors>"


class ContractCollection:
    """
    Коллекция контрактов из скомпилированных данных.
    Позволяет обращаться к контрактам по имени.
    """
    def __init__(self, compiled_data):
        self.contracts = {}
        self.compiled_data = compiled_data
        for full_name, data in compiled_data.get("contracts", {}).items():
            contract_name = full_name.split(":")[-1]
            self.contracts[contract_name] = Contract(contract_name, data.get("abi", []))

    def __getitem__(self, name):
        """Позволяет получить контракт через collection['MyContract']"""
        return self.contracts[name]

    def __repr__(self):
        return f"<ContractCollection {list(self.contracts.keys())}>"

    def save(self, filename: str):
        """Сохраняет ABI/байткод в JSON"""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.compiled_data, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, filename: str):
        """Загружает ABI/байткод из JSON"""
        with open(filename, "r", encoding="utf-8") as f:
            compiled_data = json.load(f)
        return cls(compiled_data)


def compile_solidity(sol_file: str, save_json: bool = True) -> dict:
    """
    Компиляция Solidity-контракта через Docker-образ ethereum/solc.
    Файл копируется во временную папку, которая монтируется в контейнер.
    """
    client = docker.from_env()

    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(Path(sol_file), Path(tmpdir) / sol_file)

        result = client.containers.run(
            "ethereum/solc:0.8.20",
            [
                "--combined-json", "abi,bin,metadata",
                f"/sources/{sol_file}"
            ],
            volumes={tmpdir: {"bind": "/sources", "mode": "rw"}},
            remove=True
        )
    compiled = json.loads(result.decode("utf-8"))
    return ContractCollection(compiled)


if __name__ == "__main__":
    sol_file = "Example.sol"
    contracts = compile_solidity(sol_file)
    contracts.save("compiled.json")
    print("Компиляция:", contracts)

    # второй запуск -> можно грузить из файла
    loaded_contracts = ContractCollection.load("compiled.json")
    print("Из файла:", loaded_contracts)
