#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compile Solidity contracts and generate Python helpers automatically.
Usage:
    python compile_and_generate.py MyToken.sol MyNFT.sol -o out_dir
"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import List
import hashlib
import docker
import tempfile
import shutil

# ---------- Helpers ----------

def _keccak256_hex(text: str) -> str:
    return "0x" + hashlib.sha3_256(text.encode()).hexdigest()

def canonical_type(type_str: str, components: List[dict] = None) -> str:
    if type_str.startswith("tuple"):
        inner = ",".join(canonical_type(c["type"], c.get("components")) for c in (components or []))
        suffix = type_str[5:]
        return f"({inner}){suffix}"
    return type_str

def method_suffix_from_types(types: List[str]) -> str:
    def norm(t: str) -> str:
        t = t.replace("(", "").replace(")", "").replace(",", "_").replace("[]", "_arr")
        t = re.sub(r"\[(\d+)\]", r"_arr\1", t)
        t = t.replace(" ", "").replace(";", "_").replace("[", "_").replace("]", "_").replace("*", "x").replace("-", "_")
        return t
    return "__" + "_".join(norm(t) for t in types) if types else ""

# ---------- Templates ----------

HEADER = '''# Auto-generated from ABI. Do not edit.
from typing import List, Any
import hashlib

def _keccak256_hex(text: str) -> str:
    return "0x" + hashlib.sha3_256(text.encode()).hexdigest()
'''

CONTRACT_TEMPLATE = '''
class {class_name}:
    """Auto-generated helpers for {class_name}."""
    abi_json: str = {abi_json!r}

    class Functions:
{functions}

    class Events:
{events}

    class Errors:
{errors}
'''

FUNCTION_TEMPLATE = '''        class {name}:
            signature: str = "{sig}"
            selector: str = _keccak256_hex(signature)
            inputs: List[str] = {types_repr}
'''

EVENT_TEMPLATE = '''        class {name}:
            signature: str = "{sig}"
            topic0: str = _keccak256_hex(signature)
            inputs: List[str] = {types_repr}
            indexed: List[bool] = {indexed_list}
'''

ERROR_TEMPLATE = '''        class {name}:
            signature: str = "{sig}"
            selector: str = _keccak256_hex(signature)
            inputs: List[str] = {types_repr}
'''

# ---------- Generation functions ----------

def generate_functions(entries: List[dict]) -> str:
    by_name = {}
    for e in entries:
        if e.get("type") == "function":
            by_name.setdefault(e.get("name", ""), []).append(e)
    lines = []
    for name, overloads in by_name.items():
        for e in overloads:
            in_types = [canonical_type(i["type"], i.get("components")) for i in e.get("inputs", [])]
            sig = f'{name}({",".join(in_types)})'
            suffix = method_suffix_from_types(in_types) if len(overloads) > 1 else ""
            py_name = f"{name}{suffix}"
            lines.append(FUNCTION_TEMPLATE.format(
                name=py_name,
                sig=sig,
                types_repr=in_types
            ))
    return "\n".join(lines) if lines else "        pass"

def generate_events(entries: List[dict]) -> str:
    lines = []
    for e in entries:
        if e.get("type") != "event":
            continue
        name = e.get("name", "Event")
        in_types = [canonical_type(i["type"], i.get("components")) for i in e.get("inputs", [])]
        sig = f'{name}({",".join(in_types)})'
        indexed_list = [i.get("indexed", False) for i in e.get("inputs", [])]
        lines.append(EVENT_TEMPLATE.format(
            name=name,
            sig=sig,
            types_repr=in_types,
            indexed_list=indexed_list
        ))
    return "\n".join(lines) if lines else "        pass"

def generate_errors(entries: List[dict]) -> str:
    lines = []
    for e in entries:
        if e.get("type") != "error":
            continue
        name = e.get("name", "Error")
        in_types = [canonical_type(i["type"], i.get("components")) for i in e.get("inputs", [])]
        sig = f'{name}({",".join(in_types)})'
        py_name = f"{name}{method_suffix_from_types(in_types) if in_types else ''}"
        lines.append(ERROR_TEMPLATE.format(
            name=py_name,
            sig=sig,
            types_repr=in_types
        ))
    return "\n".join(lines) if lines else "        pass"

def generate_module(abi: List[dict], class_name: str) -> str:
    return HEADER + CONTRACT_TEMPLATE.format(
        class_name=class_name,
        abi_json=json.dumps(abi, ensure_ascii=False),
        functions=generate_functions(abi),
        events=generate_events(abi),
        errors=generate_errors(abi)
    )

# ---------- Solidity compilation ----------

def compile_sol_files(sol_files: List[str]) -> list[dict]:
    client = docker.from_env()
    compiled_contracts = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        for f in sol_files:
            shutil.copy(Path(f), tmp_path / Path(f).name)

        for f in sol_files:
            sol_name = Path(f).name
            result = client.containers.run(
                "ethereum/solc:stable",
                ["--combined-json", "abi,bin,metadata", f"/sources/{sol_name}"],
                volumes={str(tmp_path): {"bind": "/sources", "mode": "rw"}},
                remove=True
            )
            compiled = json.loads(result.decode("utf-8"))
            for full_name, data in compiled.get("contracts", {}).items():
                contract_name = full_name.split(":")[-1]
                compiled_contracts.append({
                    "name": contract_name,
                    "abi": data.get("abi", []),
                    "bin": data.get("bin", "")
                })
    return compiled_contracts

def collect_sol_files(paths: list[str]) -> list[str]:
    """
    Возвращает список всех Solidity-файлов из переданных путей или директорий,
    включая поддиректории.
    """
    sol_files = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            # рекурсивно ищем все .sol файлы
            sol_files.extend([str(f) for f in path.rglob("*.sol")])
        elif path.is_file() and path.suffix == ".sol":
            sol_files.append(str(path))
    return sol_files

# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description="Compile Solidity and generate Python helpers")
    # parser.add_argument("sol_files", nargs="+", help="Solidity files to compile")
    parser.add_argument("paths", nargs="+", help="Solidity files or directories to compile")
    parser.add_argument("-o", "--out", type=Path, default=Path("."), help="Output directory")
    args = parser.parse_args()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    sol_files = collect_sol_files(args.paths)
    if not sol_files:
        print("No Solidity files found in the provided paths.")
        return

    print(f"Found Solidity files: {sol_files}")

    compiled_contracts = compile_sol_files(sol_files)
    # json_path = out_dir / "compiled_contracts.json"
    # json_path.write_text(json.dumps(compiled_contracts, indent=2, ensure_ascii=False), encoding="utf-8")
    # print(f"Saved compiled contracts to {json_path}")

    # generate Python files
    for contract in compiled_contracts:
        class_name = re.sub(r"[^0-9A-Za-z_]", "_", contract["name"])
        code = generate_module(contract["abi"], class_name)
        py_file = out_dir / f"{class_name}_abi.py"
        py_file.write_text(code, encoding="utf-8")
        print(f"Generated Python helper: {py_file}")

if __name__ == "__main__":
    main()
