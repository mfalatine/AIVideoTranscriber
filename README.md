# AI Video Transcriber

AI Video Transcriber is a local Windows dashboard for collecting YouTube transcripts and working with them. It can use Gemini to transcribe video audio, caption APIs to retrieve existing captions, the signed-in Antigravity provider to process downloaded audio, and Whisper or Gemini for local video files. The library, transcript edits, and AI output stay on your PC. Any transcript sent to a selected online provider or AI CLI is processed by that provider under its terms.

## Requirements

- Windows 10 or 11.
- Python 3.11 or newer, installed so `python` is available in a new PowerShell terminal.
- A Gemini API key for the minimum online setup. Google AI Studio offers a free key option; model availability, free access, rate limits, and quotas depend on Google and are not guaranteed.
- Node.js is needed to run the JavaScript tests and for the optional npm-based Codex CLI installation.

No Python packages are needed to start the app. Whisper and the optional transcript and AI integrations have their own setup below.

## Start the app

1. Download or clone this repository and extract it to a folder you can write to.
2. In PowerShell, run `python --version` and confirm it reports Python 3.11 or newer.
3. Double-click `Start.bat`. The app starts and opens in your browser at `http://127.0.0.1:8776`.
4. Open **Settings**, paste a Gemini API key, and save. Add a YouTube URL on the **Add videos** tab and retrieve its transcript.

The server listens on loopback only. Other computers on your network cannot connect to it.

### Get a Gemini API key

1. Sign in to [Google AI Studio](https://aistudio.google.com/api-keys).
2. Open **API Keys** and create a key for an available project. Import an existing Google Cloud project if AI Studio asks you to.
3. Copy the key, open **Settings** in the app, paste it into **Gemini API key**, and save.

Google explains [how Gemini API keys work](https://ai.google.dev/gemini-api/docs/api-key) and publishes current [Gemini API pricing and free-tier details](https://ai.google.dev/gemini-api/docs/pricing). A key does not guarantee that a particular model is free or that a quota will be available.

## Optional transcript services

Each service below stays unavailable until you install or configure it and select it in **Settings**.

### Supadata

1. Create or sign in to an account at the [Supadata dashboard](https://dash.supadata.ai/).
2. Create or copy an API key from the dashboard. See the [transcript API guide](https://docs.supadata.ai/get-transcript).
3. Paste the key into **Supadata API key** in **Settings**, save, then choose Supadata as the primary service, fallback, or batch mode.

The app asks Supadata for existing captions; its generated-transcript mode is not enabled.

### FetchTranscript

1. [Create an account](https://fetchtranscript.com/docs) and open its dashboard.
2. Create or copy an API key from the dashboard. The service's [getting-started guide](https://fetchtranscript.com/docs) describes account and key setup.
3. Paste the key into **FetchTranscript API key** in **Settings**, save, then choose FetchTranscript as the primary service, fallback, or batch mode.

### Antigravity CLI (`agy`)

1. Install the official [Google Antigravity CLI](https://github.com/google-antigravity/antigravity-cli) by following its Windows instructions.
2. Open PowerShell, run `agy`, and complete Google sign-in yourself. Close the CLI after sign-in; the app reuses that saved login. See Google's [CLI usage guide](https://www.antigravity.google/docs/cli/using).
3. Install `yt-dlp` into the Python interpreter used for this feature. If automatic discovery does not find that interpreter, set `AIVIDEOTRANSCRIBER_AGY_PYTHON` to its full executable path. For example, in PowerShell:

   ```powershell
   $python = Read-Host 'Full path to the Python executable for Antigravity audio downloads'
   & $python -m pip install yt-dlp
   setx AIVIDEOTRANSCRIBER_AGY_PYTHON $python
   ```

   Open a new terminal and start the app again so it receives the variable. In **Settings**, choose Antigravity as the primary service or fallback. The app downloads temporary audio and passes that file to the provider used by the signed-in Antigravity CLI. Transcription is handled online by that provider; it is not offline or on-device. The app removes the temporary audio after the attempt.

Gemini also uses `yt-dlp` for an optional video-duration lookup. It can still transcribe when that lookup is unavailable. To choose a different interpreter for this lookup, set `AIVIDEOTRANSCRIBER_YTDLP_PYTHON` to a Python executable with `yt-dlp` installed; leave it unset to use automatic per-user discovery.

### Local Whisper

Whisper is for video files selected in the app. It uses a separate Python virtual environment with [faster-whisper](https://github.com/SYSTRAN/faster-whisper); the app shows whether the environment is ready and whether it detected CPU or CUDA support. The current Windows worker loads its CUDA runtime libraries before choosing a device, so those libraries are required even on a CPU-only computer. When ready, it selects its CPU path if CTranslate2 detects no CUDA device; CPU transcription can be slow. GPU acceleration requires a compatible NVIDIA driver and CUDA setup. See the [CTranslate2 hardware guide](https://opennmt.net/CTranslate2/hardware_support.html).

In PowerShell, create the environment and install the package:

```powershell
$venv = Join-Path $env:USERPROFILE 'AIVideoTranscriberWhisper'
python -m venv $venv
& (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $venv 'Scripts\python.exe') -m pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12
setx AIVIDEOTRANSCRIBER_WHISPER_PYTHON (Join-Path $venv 'Scripts\python.exe')
```

Open a new terminal and restart the app. If the environment is stored elsewhere, set `AIVIDEOTRANSCRIBER_WHISPER_PYTHON` to its `python.exe` path instead. The first transcription downloads the Whisper model, which is about 2.9 GB.

## Optional AI tools

Formatted, Summary, and Ask AI use an installed command-line tool and its own sign-in. Install and sign in to one tool yourself, then select it in **Settings**. These tools remain unused until selected and a request is started.

### Claude Code

1. Follow Anthropic's [Windows setup instructions](https://code.claude.com/docs/en/setup) to install Claude Code. In PowerShell, the documented native installer is `irm https://claude.ai/install.ps1 | iex`.
2. Open a new terminal, run `claude`, and complete sign-in when prompted.
3. Select Claude Code in the app's **Settings**.

### Codex CLI

1. Install Node.js, then open a new terminal so `npm` is available.
2. Install the CLI with `npm install -g @openai/codex`.
3. Run `codex` in a terminal and choose **Sign in with ChatGPT** or another available sign-in method. Follow the [official Codex CLI guide](https://developers.openai.com/codex/cli).
4. Select Codex in the app's **Settings**.

### OpenCode with OpenRouter

1. Install OpenCode using its [official documentation](https://opencode.ai/docs/).
2. Start `opencode` in a terminal, enter `/connect`, choose OpenRouter, and provide an API key created in the [OpenRouter dashboard](https://openrouter.ai/).
3. Enter `/models` and choose a model currently marked free and available to your account. The available list can change; this project does not promise a particular model or quota. See OpenCode's [provider instructions for OpenRouter](https://opencode.ai/docs/providers/).
4. Select OpenCode in the app's **Settings** and choose the same model from its current model list.

## Data and API keys

The app creates a `data` folder beside the application files. It contains the local library database, saved settings and keys, timing information, and logs. Keep this folder private and back it up if you want to preserve your library.

On Windows, saved API keys in `data/settings.json` are encrypted with Windows DPAPI for the current Windows user. The application API returns only key status flags, not the key values. The app listens only on `127.0.0.1`; keep the computer account and local files protected as usual.

## Run the tests

From the repository folder, run the Python tests with Python 3.11 or newer:

```powershell
python -B -m unittest discover -s tests
```

To run the JavaScript tests, install Node.js 21 or newer and run:

```powershell
node --test tests/*.test.js
```

## License

This project is distributed under the MIT License. See [`LICENSE`](LICENSE).
