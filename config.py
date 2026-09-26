"""Central settings. Read from environment variables on every access,
so tests and the demo can override them without re-importing anything."""
import os


class _Settings:
    @property
    def db_path(self) -> str:
        return os.getenv("AGENT_DB", "agent.db")

    @property
    def llm_mode(self) -> str:
        # "ollama" = real local model | "mock" = fake model (tests/demo) | "off" = LLM disabled
        return os.getenv("LLM_MODE", "ollama").lower()

    @property
    def ollama_url(self) -> str:
        return os.getenv("OLLAMA_URL", "http://localhost:11434")

    @property
    def ollama_model(self) -> str:
        return os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

    @property
    def llm_timeout(self) -> float:
        return float(os.getenv("LLM_TIMEOUT", "60"))

    @property
    def llm_min_confidence(self) -> float:
        return float(os.getenv("LLM_MIN_CONFIDENCE", "0.6"))

    @property
    def vendor_ack_minutes(self) -> float:
        return float(os.getenv("VENDOR_ACK_MINUTES", "15"))

    @property
    def max_vendor_attempts(self) -> int:
        return int(os.getenv("MAX_VENDOR_ATTEMPTS", "2"))

    @property
    def summary_max_chars(self) -> int:
        return int(os.getenv("SUMMARY_MAX_CHARS", "400"))

    @property
    def telegram_token(self) -> str:
        return os.getenv("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self) -> str:
        return os.getenv("TELEGRAM_CHAT_ID", "")


settings = _Settings()
