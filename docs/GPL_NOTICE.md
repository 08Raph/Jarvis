# GPL-licensed components in the natural-voice build

Installers built with natural voices (`build_installer.bat` uses `-WithKokoro`) contain these components, which are licensed under the **GNU General Public License version 3**
(GPL-3.0):

| Component | What it does here | Where its source is |
|---|---|---|
| **espeak-ng** (shipped inside the `espeakng-loader` package: a library and data files) | turns text into speech sounds for the Kokoro voices | the project pages linked from https://pypi.org/project/espeakng-loader/ |
| **phonemizer-fork** (Python package `phonemizer`) | calls espeak-ng from Python | the project pages linked from https://pypi.org/project/phonemizer-fork/ |

Other parts of the natural-voice engine are under permissive licences (kokoro-onnx: MIT; ONNX Runtime: MIT; the Kokoro-82M voice model: Apache-2.0, downloaded by you on the Voice page).

## Your rights
* The full licence text is at https://www.gnu.org/licenses/gpl-3.0.txt .
* These components are separate files in the application folder (`_internal`). You may replace them with your own build of the same libraries.
* **Source offer:** the complete corresponding source of these components is available from the pages above. If you cannot get it there, write to the person or company that gave you this
  installer and ask for it; they must provide it for at least three years from when you received this installer.
* No warranty is given for these components, as the licence states.

## For the publisher (not legal advice)
GPL-3.0 can have consequences for how the rest of a program that combines with GPL code may be distributed. This project has **not** had a legal review of that question.
The natural-voice engine is therefore included only in builds that ask for it (`-WithKokoro`); a build without that switch contains none of these components. Before selling
an installer that includes them, get qualified advice.
