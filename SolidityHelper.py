from typing import Optional, Iterator, List, Dict

import docker, json
import os, subprocess
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

    def __init__(self, type_str: str, components: list = None, is_input=True):
        self.raw = type_str  # строка типа из ABI: "uint256", "tuple", "tuple[]"
        self.components = [
            (ABIInput if is_input else ABIOutput)(
                c.get("name", ""),
                c["type"],
                c.get("indexed", False),
                c.get("components")
            )
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


class ABIParameter:
    """Базовый параметр ABI"""

    def __init__(self, name: str, type_: str, components=None):
        self.name = name
        self.type = ABIType(type_, components, is_input=self.__class__ is ABIInput)

    def __repr__(self):
        return f"{self.type.canonical_type} {self.name}".strip()


class ABIInput(ABIParameter):
    """Входной параметр функции/события"""
    def __init__(self, name, type_: str, indexed=False, components=None):
        super().__init__(name, type_, components)
        self.indexed = indexed


class ABIOutput(ABIParameter):
    """Выходной параметр функции"""
    def __init__(self, name, type_: str, components=None):
        super().__init__(name, type_, components)


class ABIEntry:
    """Базовый класс для элементов ABI."""

    def __init__(self, name, inputs, entry_type):
        self.name = name
        self.inputs: list[ABIInput] = inputs
        self.entry_type = entry_type

    @property
    def signature(self):
        raise NotImplementedError


class FunctionABI(ABIEntry):
    """Функция контракта."""

    def __init__(self, name, inputs, outputs, state_mutability):
        super().__init__(name, inputs, "function")
        self.outputs: list[ABIOutput] = outputs
        self.state_mutability = state_mutability

    @property
    def signature(self):
        types = ",".join(inp.type.canonical_type for inp in self.inputs)
        return f"{self.name}({types})"

    @property
    def selector(self):
        return keccak256(self.signature)[:8]

    def encode_abi(self, args):
        types = [inp.type for inp in self.inputs]
        encoded_args = abi_encode(types, args)
        return self.selector + encoded_args.hex()


class EventABI(ABIEntry):
    """Событие контракта."""

    def __init__(self, name, inputs, anonymous=False):
        super().__init__(name, inputs, "event")
        self.anonymous = anonymous

    @property
    def signature(self):
        types = ",".join(inp.type.canonical_type for inp in self.inputs)
        return f"{self.name}({types})"

    @property
    def selector(self):
        return keccak256(self.signature)


class ConstructorABI(ABIEntry):
    """Конструктор контракта."""

    def __init__(self, inputs, state_mutability="nonpayable"):
        super().__init__("", inputs, "constructor")
        self.state_mutability = state_mutability

    @property
    def signature(self):
        return None


class ErrorABI(ABIEntry):
    """Пользовательская ошибка."""

    def __init__(self, name, inputs):
        super().__init__(name, inputs, "error")

    @property
    def signature(self):
        types = ",".join(inp.type.canonical_type for inp in self.inputs)
        return f"{self.name}({types})"

    @property
    def selector(self):
        return keccak256(self.signature)[:10]


class ABIEntryFactory:
    """Фабрика для создания правильного наследника ABIEntry из ABI dict"""

    @staticmethod
    def create(entry: dict) -> ABIEntry:
        entry_type = entry.get("type")
        name = entry.get("name", "")

        inputs = [
            ABIInput(i.get("name", ""), i["type"], i.get("indexed", False), i.get("components"))
            for i in entry.get("inputs", [])
        ]

        outputs = [
            ABIOutput(o.get("name", ""), o["type"], o.get("components"))
            for o in entry.get("outputs", [])
        ] if "outputs" in entry else []

        state_mutability = entry.get("stateMutability", "")

        if entry_type == "function":
            return FunctionABI(name, inputs, outputs, state_mutability)

        elif entry_type == "event":
            return EventABI(name, inputs, entry.get("anonymous", False))

        elif entry_type == "constructor":
            return ConstructorABI(inputs, state_mutability)

        elif entry_type == "error":
            return ErrorABI(name, inputs)

        elif entry_type == "fallback":
            return ABIEntry("", [], "fallback")

        elif entry_type == "receive":
            return ABIEntry("", [], "receive")

        return ABIEntry(name, inputs, entry_type)  # запасной вариант


class Contract:
    """
    Представляет скомпилированный контракт:
    - функции
    - события
    - конструкторы
    - fallback/receive
    - ошибки
    Доступ к функциям/событиям/ошибкам возможен как через методы (get_function),
    так и через атрибуты (contract.transfer).
    """

    def __init__(self, name: str, abi: list[dict]) -> None:
        self.name: str = name
        self._entries: List[ABIEntry] = []

        self._functions: Dict[str, FunctionABI] = {}
        self._events: Dict[str, EventABI] = {}
        self._constructors: List[ConstructorABI] = []
        self._fallbacks: List[ABIEntry] = []
        self._receives: List[ABIEntry] = []
        self._errors: Dict[str, ErrorABI] = {}

        self._parse_abi(abi)
        self.abi: str = json.dumps(abi)

    def _parse_abi(self, abi: list[dict]) -> None:
        for raw_entry in abi:
            abi_entry: ABIEntry = ABIEntryFactory.create(raw_entry)
            self._entries.append(abi_entry)

            if isinstance(abi_entry, FunctionABI):
                self._functions[abi_entry.name] = abi_entry
            elif isinstance(abi_entry, EventABI):
                self._events[abi_entry.name] = abi_entry
            elif isinstance(abi_entry, ConstructorABI):
                self._constructors.append(abi_entry)
            elif isinstance(abi_entry, ErrorABI):
                self._errors[abi_entry.name] = abi_entry
            elif abi_entry.entry_type == "fallback":
                self._fallbacks.append(abi_entry)
            elif abi_entry.entry_type == "receive":
                self._receives.append(abi_entry)

    def get_function(self, name: str) -> Optional["FunctionABI"]:
        return self._functions.get(name)

    def get_event(self, name: str) -> Optional["EventABI"]:
        return self._events.get(name)

    def get_error(self, name: str) -> Optional["ErrorABI"]:
        return self._errors.get(name)

    def get_constructor(self) -> Optional["ConstructorABI"]:
        return self._constructors[0] if self._constructors else None

    def __iter__(self) -> Iterator["ABIEntry"]:
        return iter(self._entries)

    def __getattr__(self, name: str) -> ABIEntry:
        """Доступ к функциям/событиям/ошибкам через точку"""
        if name in self._functions:
            return self._functions[name]
        if name in self._events:
            return self._events[name]
        if name in self._errors:
            return self._errors[name]
        raise AttributeError(f"'{self.name}' contract has no member '{name}'")

    def __repr__(self) -> str:
        return f"<Contract {self.name}: {len(self._functions)} funcs, {len(self._events)}"


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
