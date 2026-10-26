# Сторонние компоненты Peregon и их лицензии

Peregon (© XClassiK) распространяется под лицензией **GNU GPL версии 3 или любой более поздней**
(`GPL-3.0-or-later`), полный текст — в файле [`LICENSE`](LICENSE).

Готовая сборка (`Peregon-<версия>-win64.zip`) содержит сторонние библиотеки. Ниже перечислено всё,
что в неё попадает: пакеты Python и нативные библиотеки внутри них. Тексты лицензий лежат в папке
[`licenses/`](licenses/) (в сборке — рядом с `Peregon.exe`):

- `licenses/<пакет>-<версия>/` — файлы лицензий, скопированные без изменений из метаданных
  (`*.dist-info`) установленного пакета;
- `licenses/common/` — полные тексты типовых лицензий: `GPL-3.0.txt`, `GPL-2.0.txt`, `LGPL-3.0.txt`,
  `LGPL-2.1.txt`, `MPL-2.0.txt`, `Apache-2.0.txt`, `GCC-exception-3.1.txt`, `PSF-2.0-Python.txt`
  (источник каждого — см. раздел «Откуда взяты тексты» в конце).

Перечень составлен по фактическому содержимому сборки 0.1.0 (`dist/Peregon/_internal` и архив
модулей внутри `Peregon.exe`). Где лицензию или версию нельзя было установить по файлам пакета,
так и написано: **«не установлено»**.

## 1. Пакеты Python в сборке

| Пакет | Версия | Лицензия (SPDX) | Исходный код этой версии | Тексты |
|---|---|---|---|---|
| CPython (интерпретатор, `python313.dll`, стандартная библиотека) | 3.13.17 | PSF-2.0 | https://www.python.org/downloads/source/ | `common/PSF-2.0-Python.txt` |
| PySide6-Essentials (Qt for Python) | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only (используется на условиях LGPL-3.0) | https://download.qt.io/official_releases/QtForPython/ , https://code.qt.io/cgit/pyside/pyside-setup.git/ (версия 6.12.0); на PyPI sdist нет | `common/LGPL-3.0.txt`, `common/GPL-3.0.txt` ¹ |
| shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | те же, что у PySide6 | `common/LGPL-3.0.txt` ¹ |
| av (PyAV) | 19.0.2 | BSD-3-Clause (собственный код; встроенный FFmpeg — см. раздел 2) | https://files.pythonhosted.org/packages/23/01/76b6bf563fe907597cf58f10d10168e93fe588b6db9f2658db87e87efa7e/av-19.0.2.tar.gz | `av-19.0.2/` |
| Pillow | 13.0.0 | MIT-CMU (встроенные библиотеки — см. раздел 2) | https://files.pythonhosted.org/packages/ce/a4/9cc30690a394a8305e8be4c9036aa16d0dfddadd09079860b582f562496d/pillow-13.0.0.tar.gz | `pillow-13.0.0/` |
| pillow-heif | 1.8.1 | BSD-3-Clause (код); колесо целиком — GPLv2 по заявлению авторов (из-за x265) | https://files.pythonhosted.org/packages/44/c1/82145984920ca055675af2c2795bd30da6f7461215c41f3c1eacb3d66353/pillow_heif-1.8.1.tar.gz | `pillow_heif-1.8.1/` |
| rawpy | 0.27.1 | MIT (код); LibRaw — LGPL-2.1 (см. раздел 2) | https://files.pythonhosted.org/packages/f3/ae/c1c7816ed3f3cbf7ca284a37371243500f4eb39b6a32f7368b585c4ef5c4/rawpy-0.27.1.tar.gz | `rawpy-0.27.1/` |
| numpy (нужен rawpy) | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (встроенный OpenBLAS — см. раздел 2) | https://files.pythonhosted.org/packages/95/b0/c7453d0b6e2073c3264468b106ee1563750cecc910965e67357e3698c83e/numpy-2.5.4.tar.gz | `numpy-2.5.4/` |
| resvg-py | 0.5.0 | MIT (сама resvg и её Rust-зависимости — см. раздел 2) | https://files.pythonhosted.org/packages/2a/64/a24f8f29d8bf158e01f6ccad68a1366afd922dc0f0977cbd0c0aaa7a22f2/resvg_py-0.5.0.tar.gz | `resvg_py-0.5.0/` |
| pypdfium2 (+ pypdfium2_raw) | 5.14.0 | BSD-3-Clause, Apache-2.0 (PDFium и его зависимости — см. раздел 2) | https://files.pythonhosted.org/packages/95/d0/c81d3a7c2a9af37b817ace1de0acd40cf44d15f12407c5e86b3668364a5c/pypdfium2-5.14.0.tar.gz | `pypdfium2-5.14.0/` |
| pikepdf | 10.17.0 | MPL-2.0 (qpdf и др. — см. раздел 2) | https://files.pythonhosted.org/packages/b8/c3/8fc5695947a711263e17786c1d913b0cea660a1b174a9e0e944ad037732c/pikepdf-10.17.0.tar.gz | `pikepdf-10.17.0/` |
| fpdf2 | 2.8.9 | LGPL-3.0-only | https://files.pythonhosted.org/packages/12/23/84dbe637708c2690972eff5df233a7c9f8d4bde809f714839dc1b08f5e5e/fpdf2-2.8.9.tar.gz | `fpdf2-2.8.9/` |
| fonttools (нужен fpdf2) | 4.67.0 | MIT | https://files.pythonhosted.org/packages/94/36/102e180f8f5dbaee88b26595b01ca8aa80bf4e62128d9aa94265b3996c96/fonttools-4.67.0.tar.gz | `fonttools-4.67.0/` |
| magika | 1.0.3 | Apache-2.0 | https://files.pythonhosted.org/packages/00/be/fa7d512ec15763ad7d8ba083e39bb445fa969ce5159361fa449a02569cf9/magika-1.0.3.tar.gz | `common/Apache-2.0.txt` ² |
| onnxruntime (нужен magika) | 1.31.0 | MIT (сторонние части — `ThirdPartyNotices.txt`) | https://github.com/microsoft/onnxruntime (версия 1.31.0); на PyPI sdist нет | `onnxruntime-1.31.0/` |
| puremagic | 2.2.0 | MIT | https://files.pythonhosted.org/packages/24/74/ce5987ab9b8aec4ced06e2723ebb604205c9eb58abdad91453da93166380/puremagic-2.2.0.tar.gz | `puremagic-2.2.0/` |
| lxml | 6.1.3 | BSD-3-Clause (встроенные libxml2/libxslt/zlib/iconv — см. раздел 2) | https://files.pythonhosted.org/packages/23/ad/28ecd7cb894d172f3c9c80a075eeeb2017ac62e3632cee05a5f9493547eb/lxml-6.1.3.tar.gz | `lxml-6.1.3/` |
| defusedxml | 0.7.1 | PSF-2.0 | https://files.pythonhosted.org/packages/0f/d5/c66da9b79e5bdb124974bfe172b4daf3c984ebd9c2a06e2b8a4dc7331c72/defusedxml-0.7.1.tar.gz | `defusedxml-0.7.1/` |
| openpyxl | 3.1.5 | MIT | https://files.pythonhosted.org/packages/3d/f9/88d94a75de065ea32619465d2f77b29a0469500e99012523b91cc4141cd1/openpyxl-3.1.5.tar.gz | `openpyxl-3.1.5/` |
| et-xmlfile (нужен openpyxl) | 2.0.0 | MIT | https://files.pythonhosted.org/packages/d3/38/af70d7ab1ae9d4da450eeec1fa3918940a5fafb9055e934af8d6eb0c2313/et_xmlfile-2.0.0.tar.gz | `et_xmlfile-2.0.0/` |
| python-docx | 1.2.0 | MIT | https://files.pythonhosted.org/packages/a9/f7/eddfe33871520adab45aaa1a71f0402a2252050c14c7e3009446c8f4701c/python_docx-1.2.0.tar.gz | `python_docx-1.2.0/` |
| typing-extensions (нужен python-docx) | 4.16.0 | PSF-2.0 | https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz | `typing_extensions-4.16.0/` |
| Markdown | 3.11.2 | BSD-3-Clause | https://files.pythonhosted.org/packages/f4/32/0b4d0297064244f04af4331b65de16237cc3cce07474171ee14222a28981/markdown-3.11.2.tar.gz | `markdown-3.11.2/` |
| beautifulsoup4 | 4.15.0 | MIT | https://files.pythonhosted.org/packages/43/65/318323f98dbee45d42dff61d8f047181bc6f2268a9068cfad035a46be5af/beautifulsoup4-4.15.0.tar.gz | `beautifulsoup4-4.15.0/` |
| soupsieve (нужен beautifulsoup4) | 3.0.4 | MIT | https://files.pythonhosted.org/packages/19/63/0b6bae54546aed98d72f12d09df57b19a287f01c84d22047fa2e23772ab9/soupsieve-3.0.4.tar.gz | `soupsieve-3.0.4/` |
| charset-normalizer | 3.5.2 | MIT | https://files.pythonhosted.org/packages/33/1c/f41d4e74c28ab327ff3acd36053f7ea506c55872d7a90b0fa71aa3ab0c89/charset_normalizer-3.5.2.tar.gz | `charset_normalizer-3.5.2/` |
| pycryptodomex | 3.24.1 | BSD-2-Clause и public domain (по файлу `LICENSE.rst`) | https://files.pythonhosted.org/packages/f4/e7/50e72301e32acb73a314e663c2014e1ddefc019d5f434ff14ec89861d664/pycryptodomex-3.24.1.tar.gz | `pycryptodomex-3.24.1/` |
| psutil | 7.2.2 | BSD-3-Clause | https://files.pythonhosted.org/packages/aa/c6/d1ddf4abb55e93cebc4f2ed8b5d6dbad109ecb8d63748dd2b20ab5e57ebe/psutil-7.2.2.tar.gz | `psutil-7.2.2/` |
| PyInstaller (загрузчик внутри `Peregon.exe`) | 6.22.3 | GPL-2.0-or-later с исключением для загрузчика (разрешает распространять собранные программы на любых условиях) | https://files.pythonhosted.org/packages/63/41/f90302845945abd4ed647933ff5ee7c6ac93983187be67f897b6cb613331/pyinstaller-6.22.3.tar.gz | `pyinstaller-6.22.3/` |

¹ В колёсах PySide6-Essentials и shiboken6 из лицензий лежит только `LicenseRef-Qt-Commercial.txt`
(скопирован в `licenses/pyside6_essentials-6.12.0/`). Peregon использует Qt и PySide6 на условиях
**LGPL-3.0**; тексты LGPL-3.0 и GPL-3.0 взяты из других пакетов (см. конец файла) — это стандартные тексты FSF.

² В колесе magika нет текста лицензии самой библиотеки; есть только SBOM утилиты командной строки
(`licenses/magika-1.0.3/sboms/magika-cli.cyclonedx.json`), а сама утилита в сборку не входит.
Лицензия Apache-2.0 взята из метаданных пакета, текст — `common/Apache-2.0.txt`. В сборку входит
модель Magika (`_internal/magika/models/…`), она распространяется в составе того же пакета.

**Не входят в сборку**, хотя установлены в среде разработки: `py7zr` и его зависимости (`pyppmd`,
`pybcj`, `inflate64`, `multivolumefile`, `texttable`, `backports.zstd`, `brotli`) — исключены
в `packaging/konverter.spec`; `markdownify` (в коде не используется); инструменты разработки и тестов
(`pytest`, `pytest-qt`, `pluggy`, `iniconfig`, `pygments`, `colorama`, `packaging`, `setuptools`,
`altgraph`, `pefile`, `pywin32-ctypes`, `pyinstaller-hooks-contrib`); `flatbuffers`, `protobuf`,
`click`, `six` в архиве модулей сборки отсутствуют.

## 2. Нативные библиотеки внутри пакетов

### 2.1. FFmpeg и кодеки (пакет `av`, папка `_internal/av.libs`)

FFmpeg 9.0.2 (libavcodec 63.1.102), собран авторами PyAV для официального колеса. FFmpeg сообщает о себе
«LGPL version 3 or later», но в сборку **включены библиотеки под GPL** (x264, x265), поэтому
итоговое сочетание распространяется на условиях GPL — это одна из причин, по которой Peregon
выпускается под GPL-3.0-or-later.

Строка конфигурации FFmpeg (из `av`): `--enable-version3 --enable-libdav1d --enable-libmp3lame
--enable-libopus --enable-libsvtav1 --enable-libvmaf --enable-libvpx --enable-libwebp --enable-zlib
--enable-libx265 --enable-libx264 --enable-nvenc --enable-nvdec --enable-amf --enable-libvpl
--enable-mediafoundation …`.

В колесе `av` нет файлов лицензий ни FFmpeg, ни кодеков (только BSD-лицензия самого PyAV). Поэтому
лицензии ниже **по файлам колеса не установлены**; в скобках — лицензия, которую указывает сам
проект-источник (справочно, для конкретной вшитой версии не проверялась).

| Библиотека (файл в `av.libs`) | Версия | Лицензия |
|---|---|---|
| FFmpeg: avcodec-63, avformat-63, avutil-61, avfilter-12, avdevice-63, swscale-10, swresample-7 | 9.0.2 | LGPL-3.0-or-later по сообщению самого FFmpeg (`--enable-version3`); в сочетании с x264/x265 — GPL |
| x264 (`libx264-165-….dll`) | коммит `b35605ace3ddf7c1a5d67a2eb553f034aef41d55` (API 165) — по сценариям сборки pyav-ffmpeg 9.0.2-1 (`scripts/pkg.py`); в самой DLL номера ревизии нет | GPL-2.0-or-later (лицензия проекта x264) |
| x265 (`libx265-….dll`) | 4.3 (строка версии в DLL: `4.3+1-e9b8812`) | GPL-2.0-or-later (проект x265: заголовки исходников «version 2 … or (at your option) any later version», проверено 26.10.2026) |
| libvpx (`libvpx-1-….dll`) | не установлено | не установлено (проект: BSD-3-Clause) |
| Opus (`libopus-0-….dll`) | не установлено | не установлено (проект: BSD-3-Clause) |
| LAME (MP3) — отдельной DLL нет, вшит статически в avcodec (`--enable-libmp3lame`, кодер `libmp3lame`) | форк **lamer 3.101.0** (https://github.com/basswood-io/lamer, тег v3.101.0) — по сценариям сборки pyav-ffmpeg 9.0.2-1 | LGPL-2.0-or-later (лицензия LAME, унаследованная форком) |
| dav1d (`libdav1d-….dll`) | не установлено | не установлено (проект: BSD-2-Clause) |
| SVT-AV1 (`libSvtAv1Enc-….dll`) | не установлено | не установлено |
| libvmaf (`libvmaf-….dll`) | не установлено | не установлено |
| libvpl (`libvpl-….dll`) | не установлено | не установлено |
| libwebp, libsharpyuv, libwebpmux | не установлено | не установлено (проект: BSD-3-Clause) |
| zlib (`zlib1-….dll`) | не установлено | не установлено (проект: Zlib) |
| GNU libiconv (`libiconv-2-….dll`, взята сборщиком из MSYS2 MINGW64) | 1.19 (по экспортируемой переменной `_libiconv_version` в DLL; пакет MSYS2 `mingw-w64-x86_64-libiconv` 1.19-1) | LGPL-2.1-or-later (библиотека GNU libiconv) |
| Среда MinGW-w64: libgcc_s_seh-1, libstdc++-6, libwinpthread-1 | не установлено | не установлено по файлам `av`; для тех же библиотек в pillow-heif указано: GPL-3.0 с GCC Runtime Library Exception (libgcc, libstdc++), MIT и BSD (libwinpthread) |
| Поддержка NVENC/NVDEC и AMF (только заголовки при сборке FFmpeg) | — | не установлено |

Кодека AOM (libaom) в сборке нет — его DLL отсутствует.

Исходный код: PyAV 19.0.2 — ссылка в разделе 1; FFmpeg — https://ffmpeg.org/download.html
(архив версии 9.0.2: https://ffmpeg.org/releases/); сценарии сборки FFmpeg для колёс PyAV —
https://github.com/PyAV-Org/pyav-ffmpeg. Колесо `av` 19.0.2 собрано с готовым FFmpeg из релиза
pyav-ffmpeg **9.0.2-1** (файл `scripts/ffmpeg-latest.json` на теге PyAV v19.0.2); версии x264, LAME и
других кодеков выше взяты из `scripts/pkg.py` этого релиза и сверены со строками версий в DLL, где они есть.
Исходники всех компонентов под GPL/LGPL из этой папки — в архиве `Peregon-<версия>-sources.zip` (раздел 4).

### 2.2. HEIC (пакет `pillow-heif`, файлы в корне `_internal`)

Сведения из `licenses/pillow_heif-1.8.1/LICENSES_bundled.txt` (файл колеса):

| Библиотека | Версия | Лицензия | Исходный код |
|---|---|---|---|
| libheif (`libheif-….dll`) | 1.23.6 | LGPLv3 | https://github.com/strukturag/libheif/tree/v1.23.6 |
| libde265 (`libde265-0-….dll`) | 1.1.3 | LGPLv3 | https://github.com/strukturag/libde265/tree/v1.1.3 |
| x265 (`libx265-217-….dll`) | 4.3 | GPL-2.0-or-later (в колесе указано «GPLv2»; заголовки исходников x265 — «or any later version», совместимо с GPL-3.0) | https://github.com/Multicorewareinc/x265/tree/4.3 |
| MinGW-w64 runtime: libgcc_s_seh-1, libstdc++-6, libwinpthread-1 | не установлено | GPL-3.0 с GCC Runtime Library Exception (libgcc, libstdc++); MIT и BSD (libwinpthread) | https://sourceforge.net/p/mingw-w64/mingw-w64/ |

Авторы pillow-heif указывают лицензию колеса как «GPLv2»; уточнение «или более поздней» в этом файле
не приведено.

### 2.3. Pillow (`_internal/PIL`)

Встроенные библиотеки — по SBOM колеса (`licenses/pillow-13.0.0/sboms/pillow-13.0.0.cdx.json`);
тексты — в `licenses/pillow-13.0.0/LICENSE`. В сборку попали модули `_imaging`, `_imagingcms`,
`_webp`, `_avif`; модуль шрифтов `_imagingft` (FreeType, HarfBuzz, raqm, FriBiDi) в сборку не входит.

| Библиотека | Версия | Лицензия |
|---|---|---|
| libjpeg-turbo | 3.2.0 | IJG AND BSD-3-Clause (по SBOM: IJG, BSD-3-Clause) |
| zlib (zlib-ng) | 2.3.3 | Zlib |
| libtiff | 4.7.2 | libtiff |
| OpenJPEG | 2.5.4 | BSD-2-Clause |
| libwebp | 1.6.0 | BSD-3-Clause |
| libavif | 1.4.2 | BSD-2-Clause; кодеки AV1 внутри `_avif` — не установлено |
| Little CMS 2 | 2.19.1 | MIT |
| libimagequant | 4.4.1 по SBOM (GPL-3.0-or-later) | по `PIL.features` в этой сборке недоступна |

### 2.4. Остальные

| Компонент | Где | Версия | Лицензия | Тексты / исходный код |
|---|---|---|---|---|
| Qt 6 (QtCore, QtGui, QtWidgets, QtSvg + плагины platforms/styles/imageformats/iconengines) | `_internal/PySide6/Qt6*.dll`, `_internal/PySide6/plugins/` | 6.12.0 | LGPL-3.0-only (выбрана из вариантов LGPL-3.0 / GPL-2.0 / GPL-3.0) | `common/LGPL-3.0.txt`, `common/GPL-3.0.txt`; исходники: https://download.qt.io/official_releases/qt/ (версия 6.12.0), https://code.qt.io/ . Сторонние библиотеки внутри Qt (форматы картинок и т. п.) — не установлено: в колесе их перечня нет |
| PDFium | `_internal/pypdfium2_raw/pdfium.dll` | 156.0.8076.0 | BSD-3-Clause и Apache-2.0; зависимости — abseil, agg23, fast_float, FreeType, ICU, lcms, libjpeg-turbo, OpenJPEG, libpng, llvm-libc, simdutf, zlib | `licenses/pypdfium2-5.14.0/data/windows_x64/BUILD_LICENSES/`; исходники: https://pdfium.googlesource.com/pdfium/ , сборка — https://github.com/bblanchon/pdfium-binaries |
| qpdf | `_internal/pikepdf.libs/qpdf30-….dll` | 12.4.2 (по `pikepdf.__libqpdf_version__`; в README колеса указано 12.4.1) | Apache-2.0; внутри статически: libjpeg-turbo (IJG + BSD-3-Clause + Zlib), OpenSSL 3.6.0 (Apache-2.0), zlib 1.3.1 (Zlib) | `licenses/pikepdf-10.17.0/third-party-licenses/`; исходники: https://github.com/qpdf/qpdf |
| LibRaw | `_internal/rawpy/raw_r.dll` | 0.22.1 | LGPL-2.1 (файл `LICENSE.LibRaw` колеса rawpy; сам проект LibRaw также предлагает CDDL-1.0 — по файлам колеса не подтверждено). Прочие библиотеки внутри `raw_r.dll` — не установлено | `licenses/rawpy-0.27.1/LICENSE.LibRaw`; исходники: https://github.com/LibRaw/LibRaw |
| OpenBLAS (+ LAPACK, среда GCC/gfortran) | `_internal/numpy.libs/libscipy_openblas64_-….dll` | не установлено | OpenBLAS — BSD-3-Clause; LAPACK — BSD-3-Clause-Open-MPI; среда GCC — GPL-3.0-or-later WITH GCC-exception-3.1 | `licenses/numpy-2.5.4/LICENSE.txt` |
| resvg 0.48.1, usvg, tiny-skia и ещё ~70 библиотек Rust | `_internal/resvg_py/resvg_py.pyd` | по SBOM | MIT / Apache-2.0 / BSD / Zlib / Unicode-3.0 и др. — по каждой в SBOM | `licenses/resvg_py-0.5.0/sboms/resvg_py.cyclonedx.json` |
| ONNX Runtime | `_internal/onnxruntime/capi/onnxruntime*.dll` | 1.31.0 | MIT; сторонние части — по `ThirdPartyNotices.txt` | `licenses/onnxruntime-1.31.0/` |
| libxml2 2.11.9, libxslt 1.1.45, libexslt, zlib, iconv (статически в lxml) | `_internal/lxml/*.pyd` | см. слева | libxml2/libxslt — MIT; zlib — Zlib; iconv — LGPL-2.1 | `licenses/lxml-6.1.3/LICENSES.txt`, `common/LGPL-2.1.txt` |
| OpenSSL (модули `_ssl`, `_hashlib` Python) | `_internal/libcrypto-3-x64.dll`, `libssl-3-x64.dll` | 3.5.9 | Apache-2.0 | `licenses/pikepdf-10.17.0/third-party-licenses/openssl.txt` (тот же текст лицензии OpenSSL 3), `common/Apache-2.0.txt` |
| libffi (модуль `_ctypes` Python) | `_internal/libffi-8.dll` | не установлено | не установлено по файлам проекта (проект libffi: MIT) | текст в проекте не найден |
| Прочие библиотеки внутри CPython (expat, bzip2, xz, zlib и др.) | `python313.dll`, `*.pyd` | — | не установлено по файлам проекта | текст в проекте не найден |
| Microsoft Visual C++ Runtime | `msvcp140*.dll`, `vcruntime140*.dll` (в корне `_internal`, в `PySide6`, `numpy.libs`, `pikepdf.libs`) | 14.x | Microsoft Distributable Code (распространяется вместе с программами по условиям Microsoft) | `licenses/pikepdf-10.17.0/third-party-licenses/microsoft-visual-cpp-runtime.txt` |

## 3. LibreOffice — не входит в сборку

LibreOffice в архив **не входит**. Если пользователь разрешит, Peregon скачивает официальный
установочный пакет LibreOffice с сервера The Document Foundation (download.documentfoundation.org),
проверяет цифровые подписи и распаковывает его в папку программы `data\modules`.
LibreOffice распространяется под лицензией MPL-2.0 (текст — `common/MPL-2.0.txt`);
«LibreOffice» — товарный знак The Document Foundation; Peregon с этой организацией не связан.

## 4. Исходный код

**Peregon.** Полный исходный код программы, включая сценарии сборки (`packaging/`), опубликован
в репозитории https://github.com/IIDOCII/Peregon (там же — сборки в разделе Releases); код каждой версии доступен по
соответствующему тегу или архиву «Source code» в выпуске.

**Сторонние компоненты под GPL и LGPL.** Ссылки на исходный код точных версий указаны в таблицах выше
(архивы `.tar.gz` с PyPI — те же, что зафиксированы в `uv.lock` с контрольными суммами, — и репозитории
с тегами версий). Сборка использует **неизменённые** официальные колёса этих пакетов.

**Архив исходников.** К каждому выпуску прикладывается архив `Peregon-<версия>-sources.zip` с исходным
кодом точных версий всех компонентов под GPL, LGPL и MPL из этой сборки (FFmpeg, x264, x265, LAME/lamer,
GNU libiconv, libheif, libde265, Qt qtbase/qtsvg/qtimageformats, PySide6/shiboken6, LibRaw, fpdf2, pikepdf)
и сценариями сборки пакетов-обёрток (PyAV и pyav-ffmpeg, pillow-heif, rawpy, lxml). Внутри — файл
`SOURCES.md`: для каждого архива официальный адрес, sha256 и способ проверки (контрольная сумма,
подпись GPG или сверка с деревом git). Архив собирается сценариями `packaging/fetch_sources.py` и
`packaging/pack_sources.py` по списку `packaging/sources.json`.

**Письменное предложение.** В течение не менее трёх лет с даты последнего выпуска этой версии
Peregon автор (XClassiK) предоставит любому, кто об этом попросит, полный соответствующий исходный код
(Corresponding Source в смысле GPLv3 §6) всех компонентов под GPL и LGPL, входящих в эту сборку, —
включая FFmpeg, x264, x265, LAME, libheif, libde265, Qt, PySide6, LibRaw, fpdf2 и среду MinGW-w64, —
бесплатно для скачивания либо на физическом носителе по цене, не превышающей стоимость его изготовления
и пересылки. Запрос — через https://github.com/IIDOCII/Peregon/issues с указанием версии Peregon. Архив этих исходников также прикладывается
к каждому выпуску в разделе Releases (https://github.com/IIDOCII/Peregon/releases).

## 5. Замена библиотек Qt (LGPL-3.0)

LGPL-3.0 позволяет пользователю заменить библиотеку Qt своей версией (например, исправленной). Peregon
собирается в виде папки (PyInstaller, режим onedir), поэтому библиотеки Qt лежат отдельными файлами
и не упакованы внутрь `Peregon.exe`:

- `_internal\PySide6\Qt6Core.dll`, `Qt6Gui.dll`, `Qt6Widgets.dll`, `Qt6Svg.dll`;
- плагины — `_internal\PySide6\plugins\` (`platforms`, `styles`, `imageformats`, `iconengines`);
- привязки Python — `_internal\PySide6\*.pyd`, `pyside6.abi3.dll`, `_internal\shiboken6\`.

Чтобы заменить Qt, закройте Peregon и положите на место этих файлов свои, собранные из исходного кода
Qt 6.12.0 (или совместимой версии) тем же компилятором (MSVC, x64) и с теми же модулями. Если новая
версия Qt несовместима с привязками, замените вместе с ней привязки PySide6/shiboken6 той же версии.
Peregon не проверяет подписи или контрольные суммы этих файлов и не мешает такой замене.
Аналогично можно заменить и другие библиотеки под LGPL, лежащие отдельными DLL (libheif, libde265,
библиотеки FFmpeg в `_internal\av.libs`), — при условии двоичной совместимости.

## Откуда взяты тексты в `licenses/common/`

| Файл | Источник (из установленных пакетов) |
|---|---|
| `GPL-3.0.txt` (он же `LICENSE`) | pikepdf 10.17.0, `third-party-licenses/GPL-3.0.txt` — стандартный текст FSF, 674 строки, MD5 1ebbd3e34237af26da5dc08a4e440464 |
| `GPL-2.0.txt` | Pillow 13.0.0, `LICENSE` (раздел FreeType, строки 272–611) |
| `LGPL-3.0.txt` | fpdf2 2.8.9, `LICENSE` |
| `LGPL-2.1.txt` | rawpy 0.27.1, `LICENSE.LibRaw` |
| `MPL-2.0.txt` | pikepdf 10.17.0, `LICENSE.txt` |
| `Apache-2.0.txt` | pypdfium2 5.14.0, `LICENSES/Apache-2.0.txt` |
| `GCC-exception-3.1.txt` | pikepdf 10.17.0, `third-party-licenses/GCC-exception-3.1.txt` |
| `PSF-2.0-Python.txt` | typing-extensions 4.16.0, `LICENSE` (лицензия Python с историей) |
