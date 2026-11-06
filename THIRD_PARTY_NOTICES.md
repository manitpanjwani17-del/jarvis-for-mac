# Third-party notices

Jarvis for Mac's own code is under the [MIT licence](LICENSE). It downloads and uses the following, which keep their own licences. Nothing below is redistributed in this repository, except the small `assets/titanet_reference_mean.npy` noted at the end.

## AI models (downloaded at setup)

| Model | Author | Licence | Notes |
|---|---|---|---|
| Whisper Large-v3 Turbo (MLX conversion) | OpenAI; converted by mlx-community | MIT | Hearing |
| Whisper small.en, base.en (CTranslate2) | OpenAI; converted by SYSTRAN | MIT | Backup hearing; wake-word check |
| Kokoro-82M v1.0 and voices | hexgrad; ONNX by thewh1teagle (kokoro-onnx) | Apache 2.0 | Jarvis's voice: an original mix of two built-in voices |
| openWakeWord “hey jarvis”, melspectrogram, embedding models | David Scripka (openWakeWord) | **CC BY-NC-SA 4.0** | **Non-commercial use only** |
| Silero VAD (as shipped by openWakeWord) | Silero Team | MIT | Speech detection |
| TitaNet-Large (ONNX via sherpa-onnx) | NVIDIA NeMo; k2-fsa | CC BY 4.0 | Voice recognition |
| Qwen3 8B, Qwen3 4B Instruct | Alibaba Cloud, Qwen team (via Ollama) | Apache 2.0 | Brain; dictation editor |
| Qwen3.5 4B (optional) | Alibaba Cloud, Qwen team (via Ollama) | See its model page | Screen reading |

## Software

| Software | Licence | Use |
|---|---|---|
| [Ollama](https://ollama.com) | MIT | Runs the brain models (installed separately, with your consent) |
| [uv](https://github.com/astral-sh/uv) (bundled in the app) | MIT or Apache 2.0 | Installs Jarvis's Python |
| Python 3.13 (python-build-standalone) | PSF licence | Jarvis's Python |
| faster-whisper, CTranslate2 | MIT | Speech to text |
| mlx, mlx-whisper | MIT | Speech to text on Apple silicon |
| PyTorch (needed by mlx-whisper) | BSD-3-Clause | |
| kokoro-onnx | MIT | Voice |
| phonemizer, espeak-ng (via espeakng-loader) | GPL-3.0 | Pronunciation for the voice, used as separate programs |
| openWakeWord (code) | Apache 2.0 | Wake word |
| sherpa-onnx | Apache 2.0 | Voice recognition |
| onnxruntime | MIT | Runs the ONNX models |
| NumPy, SciPy, scikit-learn, numba | BSD | |
| sounddevice / PortAudio | MIT | Microphone fallback |
| certifi | MPL-2.0 | Trusted certificates for downloads |

The full list of Python packages, with exact versions, is in [`requirements.lock`](requirements.lock).

## Included in this repository

- `assets/titanet_reference_mean.npy`: 192 numbers, the average TitaNet voiceprint of the LibriSpeech dev-clean speakers (LibriSpeech: CC BY 4.0, Panayotov et al.). It's used to tell voices apart better. No audio is included.

## Names

“Jarvis” is used as a plain name. This project isn't affiliated with or endorsed by Marvel, Disney, Apple, OpenAI, Meta, WhatsApp, Alibaba, Ollama, NVIDIA or Wispr. All trademarks belong to their owners.
