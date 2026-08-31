# FE3H Localization Tool

Інструментарій для локалізації **Fire Emblem: Three Houses** (Nintendo Switch):
екстракція ігрового тексту, редактор перекладу з портретами спікерів,
реінсерт у бінарні формати гри та LayeredFS-деплой на емулятор чи консоль.

Створено для проєкту української локалізації FE3H.

**Автор:** Dmytro Bidlov («Little Bit» Team) · **Ліцензія:** MIT

> Інструмент не містить і не розповсюджує жодних ігрових ресурсів.
> Працює виключно з вашим власним легальним дампом гри.

---

## Можливості

- **Extract** — обхід усіх текстоносних файлів (DATA1 indexed entries +
  path-based `patch1–4`) з класифікацією форматів і збиранням єдиного
  `translation_bundle.txt` для перекладу. Дворівнева дедуплікація
  (entry-кластери + повторювані рядки) — кожен рядок перекладається один раз.
- **Редактор** — Electron GUI: список файлів, картки Original/Translation,
  розпізнавання спікера з `[NNNN]`-маркерів scene-діалогів (портрети,
  ім'я, voice-id), прев'ю в ігровому стилі, Ctrl+S, захист від втрати
  незбережених змін.
- **Apply** — багатопотоковий реінсерт bundle назад у формати гри з
  автопоширенням перекладу на всі дублікати та відновленням технічних
  маркерів; захист від типових помилок перекладачів (зайвий перенос рядка
  перед voice-маркером тощо).
- **Deploy** — генерація LayeredFS-оверлею (`atmosphere/contents/<TitleID>/romfs`)
  з патчем INFO0/INFO2, автодеплой у Eden / Ryujinx з автозапуском гри.
- **Шрифт** — патч G1T font-атласу + UTF8TBL-ремап для відмалювання літер,
  відсутніх у вбудованому шрифті (Є/є), через редагування DDS у Photoshop.
- **Текстури** — експорт/реінсерт multi-texture G1T (титульний екран, мапа
  монастиря) для перекладу графічних написів.
- **Прогрес** — точний лічильник перекладеного (порівняння з оригіналом
  байт-у-байт, ігноруючи хвостові пробіли).
- **Chunk-workflow** — розбиття bundle на порції для команди перекладачів
  (`tools/split_bundle.py` / `tools/merge_bundle.py`) з жорсткою валідацією
  маркерів при злитті.

## Архітектура

```
Renderer (React + TS)  ──IPC──→  Main (Electron)  ──stdio JSON-RPC──→  Python sidecar
      styles, editor,             dialogs, fs,          binary formats:
      speakers, preview           python bridge         TextS / Scene / Caption /
                                                        Credit / msgdata / G1T / DATA0-1
```

- UI: **electron-vite + React + TypeScript**
- Формати: **Python 3.11+** (без зовнішніх залежностей)
- Контракт RPC типізований у `app/shared/ipc.ts`

## Формати (реалізовано в `app/python/formats/`)

| Формат | Файли | Примітки |
|---|---|---|
| DATA0/DATA1 | архів гри | 32-байтні записи; chunked-zlib декомпресія |
| TextS (`_str.bin`) | UI-тексти,支援-діалоги | UTF-8, збереження оригінального паддінгу |
| SceneText | talk_scinario | `[NNNN]`-спікер + `＠NNNNNN` voice-маркери; строга валідація розкладки |
| Caption / Credit | субтитри відео, титри | f32-таймінги (caption); verbatim round-trip незмінених записів |
| msgdata / ScrData | 12-мовний контейнер | заміна окремого мовного слота |
| INFO0/INFO2 | patch4 | накопичувальний оверлей індексованих модів |
| G1T | текстури/шрифт | linear BC3, DDS-обмін із Photoshop |

Байтові розкладки звірені з 010-шаблонами спільноти **THRT**
(Three Houses Recompilation Tools). Серіалізатори покриті round-trip
тестами: `parse → serialize` має відтворювати оригінал байт-у-байт.

## Встановлення

```bash
git clone https://github.com/LittleBitUA/FE3H-Localization-Tool
cd FE3H-Localization-Tool/app
npm install
python tools/fetch_portraits.py        # портрети спікерів (опційно)
npm run dev                            # запуск у dev-режимі
```

Вимоги: **Node 20+**, **Python 3.11+** у PATH (або env `FE3H_PYTHON`).

### Конфігурація

Скопіюйте `fe3h-tool.config.example.json` → `fe3h-tool.config.json` у корені
репозиторію і вкажіть свої шляхи (файл не комітиться):

| Ключ | Призначення |
|---|---|
| `title_id` | TitleID гри для LayeredFS (типово FE3H) |
| `eden_exe`, `ryujinx_exe` | автозапуск емулятора після деплою |
| `game_image` | ваш дамп гри (.nsp/.xci) для автозапуску |
| `reference_mods_dir` | опційно: `mods/` наявного перекладу-референсу як фільтр «перекладабельних» entry |
| `names_json` | опційно: мапінг index→ім'я файлу (ThreeHousesFileNames.json з THRT) |

### Дамп гри

Очікувана структура: `<romfs>/DATA0.bin + DATA1.bin + patch1..4/`
(повний дамп RomFS з вашої консолі або емулятора).

## Робочий процес

1. **Open dump…** → вкажіть `romfs/`; **Set project…** → робоча тека проєкту.
2. **Scan patch + Scan DATA1** → список усіх текстоносних файлів.
3. **Extract** → `project/translation_bundle.txt` (повторний Extract зливає
   з наявним — переклади не губляться).
4. Переклад: прямо в редакторі (по файлах) або через bundle/чанки.
5. **Apply bundle** → реінсерт у `project/romfs/`.
6. **Deploy** → build LayeredFS + копія в емулятор.

### Командний переклад через чанки

```bash
python tools/split_bundle.py     # bundle → chunks/chunk_NN_<topic>.txt + _manifest.json
# … переклад чанків …
python tools/merge_bundle.py     # chunks → bundle (жорстка валідація #N-маркерів)
```

## Тести

```bash
cd app/python
python -m unittest discover -s tests            # юніт-тести форматів
FE3H_TEST_ROMFS=/path/to/romfs \
python -m unittest discover -s tests            # + round-trip по реальному дампу
```

Round-trip тести — головна страховка: будь-який дрейф серіалізатора
історично означав infinite-loading у грі.

## Подяки

- Спільноті **THRT** за 010-шаблони форматів і мапінг імен файлів.
- Fire Emblem wiki за довідкові матеріали.
