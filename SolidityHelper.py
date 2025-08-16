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


class ABIInput:
    """
    Описывает входной/выходной параметр ABI (тип, имя, признак indexed для событий).
    """
    def __init__(self, name, type_, indexed=False):
        self.name = name
        self.type = type_
        self.indexed = indexed

    def __repr__(self):
        return f"{self.type} {self.name}"


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
        types = ",".join(inp.type for inp in self.inputs)
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

    def _parse_abi(self, abi):
        for entry in abi:
            entry_type = entry.get("type")
            name = entry.get("name", "")
            inputs = [ABIInput(i.get("name", ""), i["type"], i.get("indexed", False)) for i in entry.get("inputs", [])]
            outputs = [ABIInput(o.get("name", ""), o["type"]) for o in entry.get("outputs", [])] if "outputs" in entry else []
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
        for full_name, data in compiled_data.get("contracts", {}).items():
            contract_name = full_name.split(":")[-1]
            self.contracts[contract_name] = Contract(contract_name, data.get("abi", []))

    def __getitem__(self, name):
        """Позволяет получить контракт через collection['MyContract']"""
        return self.contracts[name]

    def __repr__(self):
        return f"<ContractCollection {list(self.contracts.keys())}>"

compiled_data = json.load(open("compiled.json"))