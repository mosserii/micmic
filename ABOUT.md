# GitHub About panel

Settings the integrator applies by hand when the repository is created. GitHub does not
read them from a file. Delete this file after applying them, or keep it as the record.

## Repository

- **Name:** `micmic` (as `mosserii/micmic`; the README, CONTRIBUTING and the issue
  templates link there, so a different name means a find-and-replace of `mosserii/micmic`)
- **Description:** `Talk to your Mac. It does the thing. A voice assistant for macOS that acts instead of searching: hold right Option, say it, let go.`
- **Website:** the public MicMic site (`PUBLIC_BASE_URL` of the cloud), or
  `https://github.com/mosserii/micmic/releases/latest` until there is one.
- **Topics:** `macos`, `voice-assistant`, `menubar-app`, `speech-recognition`,
  `productivity`, `accessibility`, `automation`, `ai-assistant`, `python`, `pyobjc`,
  `apple-silicon`, `hebrew`, `multilingual`, `llm`, `gemini`, `voice-control`
- **License:** MIT (`LICENSE`); GitHub detects it and the README badge follows.
- **Include in the home page:** Releases on, Packages off, Deployments off.

## Features

- **Discussions:** on, with the categories Announcements, Q&A, Ideas, Show and tell.
  The issue template's config links there.
- **Issues:** on. **Wiki:** off (docs live in the repo). **Projects:** off.
- **Sponsorships:** off until `.github/FUNDING.yml` is filled in.

## Social preview

Settings > General > Social preview > Edit > Upload `.github/social-preview.png`
(1280 x 640). It is rendered from the real bar by `site/readme/render.py`.

## Branch protection for `main`

Require the `tests` workflow (both jobs: `assistant (keyless)` and `cloud (proxy/)`) to
pass before merging, and require one review.
