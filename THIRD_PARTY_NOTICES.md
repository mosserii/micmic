# Third-party notices

MicMic is MIT licensed (see [LICENSE](LICENSE)). The downloadable app bundles the
software below. Each package's full license text ships inside the app, next to the
package (`MicMic.app/Contents/Resources/lib/*.dist-info/` and
`MicMic.app/Contents/Resources/python/lib/python3.11/LICENSE.txt`).

## Bundled in MicMic.app

| Component | Version | License |
|---|---|---|
| [CPython](https://www.python.org/) (from [python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | 3.11 | PSF-2.0 |
| [Playwright for Python](https://github.com/microsoft/playwright-python) | 1.63.0 | Apache-2.0 |
| [PyObjC](https://github.com/ronaldoussoren/pyobjc) (core, Cocoa, ApplicationServices, CoreText, Quartz, Speech, AVFoundation, WebKit) | 12.2 | MIT |
| [greenlet](https://github.com/python-greenlet/greenlet) | 3.5.6 | MIT AND PSF-2.0 |
| [pyee](https://github.com/jfhbrook/pyee) | 13.0.1 | MIT |
| [typing_extensions](https://github.com/python/typing_extensions) | 4.16.0 | PSF-2.0 |

The Python interpreter statically includes these libraries:

| Library | License |
|---|---|
| OpenSSL | Apache-2.0 |
| SQLite | Public domain |
| libffi | MIT |
| zlib | Zlib |
| bzip2 | bzip2-1.0.6 |
| XZ Utils (liblzma) | 0BSD |
| libedit | BSD-3-Clause |
| ncurses | X11 |
| mpdecimal | BSD-2-Clause |
| Expat | MIT |

## Downloaded on first use, not bundled

| Component | License | When |
|---|---|---|
| [Node.js](https://nodejs.org/) | MIT | Fetched (sha256-pinned) the first time a task needs the browser agent. |

## Services the app talks to

These are network services, not software shipped in the app. Their own terms apply.

- **Jev** by TypeSafe AI: understands requests. Jev is a trademark of TypeSafe AI.
  MicMic is not affiliated with or endorsed by TypeSafe. See [TRADEMARKS.md](TRADEMARKS.md).
- **Google Gemini**: splits compound requests and answers knowledge questions.
- **wttr.in**: weather.
- **YouTube, Google and DuckDuckGo** public pages: finding the thing you asked to
  play or open.
- **Google Fonts**: typefaces for the MicMic window.
- **GitHub**: checking for a newer release.
- **Stripe**: Pro subscriptions, through the MicMic cloud only.
