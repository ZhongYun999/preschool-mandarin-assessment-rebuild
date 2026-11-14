"""DashScope CosyVoice adapter: one request, no implicit transport retries."""

import io
import os
import wave

from app.fixed_audio import AudioData


class TTSError(Exception):
    def __init__(self, category: str, *, uncertain: bool = False):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class DashScopeSynthesizer:
    def __init__(self, client_factory=None):
        self._factory = client_factory

    def validate(self, config: dict) -> None:
        permitted = {
            "provider",
            "model",
            "voice",
            "version",
            "speed",
            "format",
            "sample_rate",
            "region",
            "workspace",
        }
        if (
            set(config) - permitted
            or config.get("provider") != "dashscope"
            or config.get("version") != "cosyvoice-sdk-v2"
        ):
            raise TTSError("invalid_config")
        if (
            config.get("format") != "wav"
            or not isinstance(config.get("sample_rate"), int)
            or isinstance(config.get("sample_rate"), bool)
            or config["sample_rate"] <= 0
        ):
            raise TTSError("invalid_config")
        if (
            not isinstance(config.get("speed"), (int, float))
            or isinstance(config.get("speed"), bool)
            or not 0.5 <= config["speed"] <= 2
        ):
            raise TTSError("invalid_config")
        if any(
            not isinstance(config.get(k), str) or not config[k].strip() for k in ("model", "voice")
        ):
            raise TTSError("invalid_config")
        if any(
            k in config and (not isinstance(config[k], str) or not config[k].strip())
            for k in ("region", "workspace")
        ):
            raise TTSError("invalid_config")
        if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
            raise TTSError("missing_credentials")

    def synthesize(self, source_text: str, config: dict) -> AudioData:
        self.validate(config)
        import dashscope
        from dashscope import errors

        factory = self._factory or dashscope.Client
        try:
            client = factory(
                api_key=os.environ["DASHSCOPE_API_KEY"],
                max_retries=0,
                region=config.get("region"),
                workspace=config.get("workspace"),
            )
            data = client.audio.tts.synthesize(
                source_text,
                model=config["model"],
                voice=config["voice"],
                format=config["format"],
                sample_rate=config["sample_rate"],
                rate=config["speed"],
            )
        except errors.AuthenticationError:
            raise TTSError("authentication") from None
        except errors.PermissionDeniedError as exc:
            raise TTSError(
                "quota" if exc.code in {"Arrearage", "insufficient_quota"} else "permission"
            ) from None
        except errors.RateLimitError as exc:
            raise TTSError(
                "quota"
                if exc.code in {"insufficient_quota", "Throttling.AllocationQuota"}
                else "rate_limit"
            ) from None
        except errors.BadRequestError:
            raise TTSError("invalid_config") from None
        except errors.APITimeoutError:
            raise TTSError("timeout", uncertain=True) from None
        except errors.APIConnectionError:
            raise TTSError("network", uncertain=True) from None
        except Exception:  # noqa: BLE001 - never expose SDK exception text or assume charge status
            raise TTSError("provider_unknown", uncertain=True) from None
        try:
            with wave.open(io.BytesIO(data), "rb") as f:
                if f.getframerate() != config["sample_rate"] or f.getnframes() <= 0:
                    raise ValueError("invalid waveform")
                expected = f.getnframes() * f.getnchannels() * f.getsampwidth()
                if len(f.readframes(f.getnframes())) != expected:
                    raise ValueError("truncated waveform")
        except (TypeError, ValueError, EOFError, wave.Error):
            raise TTSError("invalid_audio", uncertain=True) from None
        return AudioData(data, "audio/wav")
