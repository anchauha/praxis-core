from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SYSTEM_PROMPT = (
    "You are a teaching assistant for Indiana Academic Standards in mathematics "
    "and science, grades 5 through 12. You help teachers plan lessons: unpacking "
    "standards, drafting objectives, suggesting activities and checks for "
    "understanding.\n\n"
    "You do not yet have the standards index in front of you. When a question "
    "depends on the exact wording, code, or grade placement of a standard, say "
    "plainly that you are answering from general knowledge and that the code "
    "should be verified against the index. Never invent a standard code."
)


class Settings(BaseSettings):
    """Runtime configuration. Override any field via .env or the environment."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="CATPC_",
        extra="ignore",
    )

    ollama_host: str = "http://127.0.0.1:11434"
    default_model: str = "qwen3.5:9b"

    # Context window in tokens. Ollama sizes its compute buffer from this, so on
    # an 8 GB card a 9B model needs this held down or loading fails outright.
    # It is also the budget retrieved standards will have to fit inside.
    num_ctx: int = 8192
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # Generation can run long on a local GPU; only the connect phase is short.
    request_timeout: float = 600.0
    connect_timeout: float = 5.0

    standards_index: Path = BASE_DIR / "data" / "standards_index.json"
    course_manifest: Path = BASE_DIR.parent / "standards_extract" / "course_manifest.json"
    planning_num_predict: int = 2048

    # Sessions, planning events and export manifests. Holds teacher and
    # community material, so it stays out of version control.
    store_path: Path = BASE_DIR / "data" / "catpc.sqlite"
    frontend_dir: Path = BASE_DIR / "frontend"


@lru_cache
def get_settings() -> Settings:
    return Settings()
