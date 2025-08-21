# Ethereum ABI Tools

Набор Python-классов для работы с ABI Ethereum-контрактов:
- Разбор ABI (`function`, `event`, `constructor`, `fallback`, `receive`, `error`)
- Получение сигнатур и селекторов
- Кодирование аргументов для вызовов
- Управление коллекцией контрактов из `compiled.json`

---

## Использование

### Компиляция отдельных файлов

```bash
python compile_and_generate.py MyToken.sol MyNFT.sol -o out_dir
```

### Компиляция всех контрактов в директории (включая поддиректории)

```bash
python compile_and_generate.py contracts/ -o out_dir
```

- `-o out_dir` — директория для вывода JSON и Python-файлов.
- JSON скомпилированных контрактов сохраняется как `compiled_contracts.json`.
- Для каждого контракта создается отдельный Python-файл `ContractName_abi.py`.

---

## Pipeline: Solidity → Python Helpers

```
Solidity (.sol files)
          │
          │ 1️⃣  Сбор файлов (директория или список)
          ▼
Python скрипт генерации
          │
          │ 2️⃣  Компиляция через Docker + solc
          ▼
JSON скомпилированных контрактов
          │
          │ 3️⃣  Генерация Python-файлов
          ▼
Python-хелперы для каждого контракта
  (функции, события, ошибки, селекторы)
```

### Объяснение шагов:

1. **Сбор файлов**
   - Поддерживаются отдельные `.sol` файлы и директории.
   - Рекурсивный поиск всех Solidity-файлов.

2. **Компиляция**
   - Docker образ: `ethereum/solc`.
   - Получаем JSON с ABI, байткодом и метаданными.

3. **Генерация Python**
   - Для каждого контракта отдельный `.py` файл.
   - Поля:
     - `signature` — сигнатура функции/события/ошибки
     - `selector` — первые 4 байта keccak256 для функций и ошибок
     - `topic0` — keccak256 для событий
   - Поддержка перегруженных функций.

4. **Использование**
```python
from out_dir.MyToken_abi import MyToken

# Функции
print(MyToken.Functions.transfer.signature)
print(MyToken.Functions.transfer.selector)

# События
print(MyToken.Events.Transfer.signature)
print(MyToken.Events.Transfer.signature)

# Ошибки
print(MyToken.Errors.InsufficientBalance.signature)
print(MyToken.Errors.InsufficientBalance.selector)
```

---

## Особенности

- Генерация Python-хелперов с полями:
  - `signature` — сигнатура функции/события/ошибки
  - `selector` — первые 4 байта keccak256 для функций и ошибок
  - `topic0` — keccak256 для событий
- Поддержка перегруженных функций
- Рекурсивный поиск `.sol` файлов в директориях
- Сохранение общего JSON для всех контрактов
