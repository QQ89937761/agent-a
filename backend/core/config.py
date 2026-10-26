"""配置加载层。读取 config.toml,提供强类型的配置对象。"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def scrub_feishu_secret(text: str) -> str:
    """Clear only the legacy Feishu secret, preserving all other configuration."""
    return re.sub(
        r'(?ms)(^\[feishu\][^\r\n]*\r?\n(?:(?!^\[).)*?^app_secret[ \t]*=)[^\r\n]*',
        r'\1 ""', text, count=1,
    )


SECRET_ENV_FIELDS = {
    "AGENT_LLM_API_KEY": ("llm", "api_key"),
    "AGENT_TENCENT_SECRET_ID": ("voice", "secret_id"),
    "AGENT_TENCENT_SECRET_KEY": ("voice", "secret_key"),
    "AGENT_FEISHU_APP_ID": ("feishu", "app_id"),
    "AGENT_FEISHU_APP_SECRET": ("feishu", "app_secret"),
    "AGENT_WEB_PASSWORD": ("auth", "password"),
    "AGENT_HUB_TOKEN": ("role", "hub_token"),
    "AGENT_CLOUD_BACKUP_ACCESS_KEY": ("memory", "cloud_backup", "access_key"),
    "AGENT_CLOUD_BACKUP_SECRET_KEY": ("memory", "cloud_backup", "secret_key"),
}
PROVIDER_API_KEYS_ENV = "AGENT_LLM_PROVIDER_API_KEYS"
PROVIDERS_ENV = "AGENT_LLM_PROVIDERS"
_LOGGER = logging.getLogger(__name__)
_PROVIDER_ENV_WARNING = ""
_PROVIDER_ENV_WARNED_RAW: str | None = None


@dataclass
class LLMProviderConfig:
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: list[str] = field(default_factory=list)
    context_windows: dict[str, int] = field(default_factory=dict)
    protocol: str = "openai"


@dataclass
class LLMConfig:
    provider_name: str = "默认接口"
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = ""
    chat_model: str = "gpt-4o"
    # Kept for config/API compatibility; runtime vision requests use chat_model.
    vision_model: str = ""
    models: list[str] = field(default_factory=list)
    providers: list[LLMProviderConfig] = field(default_factory=list)
    context_windows: dict[str, int] = field(default_factory=dict)
    timeout: int = 120
    protocol: str = "openai"
    # A browser save overrides deployment model defaults, never environment-managed secrets.
    settings_saved: bool = False


@dataclass
class CloudBackupConfig:
    enabled: bool = False
    endpoint: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""


@dataclass
class MemoryConfig:
    data_dir: str = ""
    backup_dir: str = ""
    backup_interval_minutes: int = 60
    backup_keep_count: int = 30
    vector_backend: str = "sqlite"
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    project_roots: list[str] = field(default_factory=list)
    max_project_files: int = 5000
    # 攒够多少轮"有价值的对话"才调一次大模型提炼(1=每轮都提炼,费钱;默认5)
    batch_turns: int = 5
    # 最多攒多久就强制提炼一次(秒),防止被强杀时丢掉这段记忆
    flush_seconds: int = 180
    cloud_backup: CloudBackupConfig = field(default_factory=CloudBackupConfig)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class AgentConfig:
    max_steps: int = 200
    confirm_before_action: bool = True
    max_history_messages: int = 20  # 短期对话记忆:最多保留最近多少条(滑窗防上下文过长)
    context_window: int = 128000  # 当前模型的上下文窗口;单轮达到 75% 时压缩旧工具结果
    open_at_login: bool = False  # 仅打包版 Electron 应用；开发态不注册系统启动项
    isolation_max_mb: int = 1024  # 过程文件/缓存隔离区上限


@dataclass
class VoiceConfig:
    provider: str = ""  # "tencent" 启用;留空则不启用云语音(兼容旧字段)
    secret_id: str = ""
    secret_key: str = ""
    region: str = "ap-guangzhou"
    tts_voice: int = 101016
    tts_speed: int = 0

    @property
    def tencent_configured(self) -> bool:
        return bool(self.secret_id and self.secret_key)

    @property
    def asr_enabled(self) -> bool:
        return self.tencent_configured

    @property
    def tts_enabled(self) -> bool:
        return self.tencent_configured

    @property
    def enabled(self) -> bool:
        return self.tencent_configured


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    allowed_open_ids: list[str] = field(default_factory=list)
    remarks: dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)


@dataclass
class AuthConfig:
    """网页访问密码。留空=不启用(本机自用时不打扰);
    远程网页必须设置密码并登录；未设密码时只允许本机访问。
    """
    password: str = ""
    password_env_managed: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.password)


@dataclass
class RoleConfig:
    """小A 跑成哪种角色:
    - standalone(默认):单机全功能,和现在一样,一台电脑什么都干。
    - hub:云端常开的"大脑+记忆+接待",不亲自操作软件,把操作类活派给 worker。
    - worker:你电脑上的"手",开机后拨号连 hub,听 hub 派活操作软件。
    """
    mode: str = "standalone"          # standalone | hub | worker
    hub_url: str = ""                 # worker 用:要连的 hub 地址,如 ws://1.2.3.4:1985/worker
    hub_token: str = ""               # hub 与 worker 之间的握手口令(防陌生人乱连)
    worker_id: str = ""                # worker 身份;留空则使用电脑名
    tls_fingerprint: str = ""          # 自签证书 SHA256 指纹;留空使用系统 CA

    @property
    def is_hub(self) -> bool:
        return self.mode == "hub"

    @property
    def is_worker(self) -> bool:
        return self.mode == "worker"

    @property
    def is_standalone(self) -> bool:
        return self.mode not in ("hub", "worker")


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    role: RoleConfig = field(default_factory=RoleConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)

    @property
    def memory_data_dir(self) -> Path:
        if self.memory.data_dir:
            return Path(self.memory.data_dir)
        return app_home() / "data" / "memory"

    @property
    def memory_backup_dir(self) -> Path:
        if self.memory.backup_dir:
            path = Path(self.memory.backup_dir).expanduser()
            return path.resolve() if path.is_absolute() else (app_home() / path).resolve()
        return (self.memory_data_dir.parent / "backup").resolve()


def _project_root() -> Path:
    # backend/core/config.py -> 项目根在上两级
    return Path(__file__).resolve().parents[2]


def app_home() -> Path:
    """可写数据根目录。打包版由 Electron 传入,开发版仍用项目目录。"""
    if value := os.getenv("XIAOA_HOME"):
        return Path(value).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = Path(os.getenv("LOCALAPPDATA", Path.home()))
        return (base / "XiaoA").resolve()
    return _project_root()


def asset_root() -> Path:
    """只读资源根目录。PyInstaller 会把资源放到 _MEIPASS。"""
    if value := os.getenv("XIAOA_ASSET_ROOT"):
        return Path(value).expanduser().resolve()
    if value := getattr(sys, "_MEIPASS", ""):
        return Path(value).resolve()
    return _project_root()


def config_path() -> Path:
    return app_home() / "config.toml"


def _known_fields(cls, values: dict) -> dict:
    """只保留 dataclass 认识的名字,过滤掉升级后已删除的旧字段(如 auto_continue_rounds)。

    否则旧 config.toml 里残留一个已删字段,构造 dataclass 时就会 TypeError,
    整个服务直接启动失败(云端 hub 就因为这个崩过)。"""
    if not isinstance(values, dict):
        return {}
    known = set(getattr(cls, "__dataclass_fields__", ()))
    return {key: value for key, value in values.items() if key in known}


def _normalize_context_windows(value: object) -> dict[str, int]:
    """Keep per-model context settings tolerant of hand-edited TOML."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for model, raw_window in value.items():
        name = str(model).strip()
        try:
            window = int(raw_window)
        except (TypeError, ValueError):
            continue
        if name and 4096 <= window <= 2_000_000:
            result[name] = window
    return result


def _normalize_tencent_credential(value: object, label: str) -> str:
    """Accept credentials copied from labelled Tencent Cloud notes."""
    text = str(value or "").strip()
    prefix = f"{label}:"
    return text[len(prefix):].strip() if text.lower().startswith(prefix.lower()) else text


def load_config(path: str | os.PathLike | None = None) -> Config:
    """加载配置。找不到 config.toml 时回退到默认值,方便首次启动。"""
    if path is None:
        path = config_path()
    path = Path(path)

    if not path.exists():
        example = asset_root() / "config.example.toml"
        if example.exists() and path == config_path():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(example.read_bytes())
        else:
            return Config()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    llm_raw = dict(raw.get("llm", {}))
    provider_raw = llm_raw.pop("providers", [])
    llm = LLMConfig(**{**LLMConfig().__dict__, **_known_fields(LLMConfig, llm_raw)})
    if isinstance(llm.models, str):
        llm.models = [model.strip() for model in llm.models.split(",") if model.strip()]
    llm.context_windows = _normalize_context_windows(llm.context_windows)
    llm.models = list(dict.fromkeys(
        str(model).strip() for model in [llm.chat_model, *llm.models] if str(model).strip()
    ))
    # The first default model is the sole model capability source, including vision.
    llm.vision_model = llm.chat_model
    llm.providers = []
    seen_provider_ids: set[str] = set()
    for index, item in enumerate(provider_raw if isinstance(provider_raw, list) else []):
        if not isinstance(item, dict):
            continue
        provider = LLMProviderConfig(**{**LLMProviderConfig().__dict__, **_known_fields(LLMProviderConfig, item)})
        provider.id = provider.id.strip() or f"provider-{index + 1}"
        if provider.id == "default" or provider.id in seen_provider_ids:
            provider.id = f"provider-{index + 1}"
        provider.name = provider.name.strip() or provider.id
        provider.models = list(dict.fromkeys(
            str(model).strip() for model in provider.models if str(model).strip()
        ))
        provider.context_windows = _normalize_context_windows(provider.context_windows)
        if provider.base_url and provider.models:
            llm.providers.append(provider)
            seen_provider_ids.add(provider.id)

    mem_raw = dict(raw.get("memory", {}))
    cloud_raw = mem_raw.pop("cloud_backup", {})
    memory = MemoryConfig(
        **{**MemoryConfig().__dict__, **_known_fields(MemoryConfig, mem_raw),
           "cloud_backup": CloudBackupConfig(**{**CloudBackupConfig().__dict__, **_known_fields(CloudBackupConfig, cloud_raw)})}
    )
    # 旧配置名保留兼容;当前统一迁移到 SQLite。
    if memory.vector_backend in ("jsonl", "chroma", "lancedb"):
        memory.vector_backend = "sqlite"
    if memory.embed_model == "text-embedding-3-small":
        memory.embed_model = MemoryConfig().embed_model

    server = ServerConfig(**{**ServerConfig().__dict__, **_known_fields(ServerConfig, raw.get("server", {}))})
    agent = AgentConfig(**{**AgentConfig().__dict__, **_known_fields(AgentConfig, raw.get("agent", {}))})
    voice = VoiceConfig(**{
        **VoiceConfig().__dict__, **_known_fields(VoiceConfig, raw.get("voice", {})),
    })
    voice.secret_id = _normalize_tencent_credential(voice.secret_id, "SecretId")
    voice.secret_key = _normalize_tencent_credential(voice.secret_key, "SecretKey")
    feishu = FeishuConfig(**{**FeishuConfig().__dict__, **_known_fields(FeishuConfig, raw.get("feishu", {}))})
    role = RoleConfig(**{**RoleConfig().__dict__, **_known_fields(RoleConfig, raw.get("role", {}))})
    auth = AuthConfig(**{**AuthConfig().__dict__, **_known_fields(AuthConfig, raw.get("auth", {}))})

    return Config(
        llm=llm, memory=memory, server=server, agent=agent, voice=voice,
        feishu=feishu, role=role, auth=auth,
    )


def plaintext_secret_env_vars(cfg: Config) -> list[str]:
    """Return env names corresponding to plaintext secrets still in config."""
    pending = []
    for env_name, path in SECRET_ENV_FIELDS.items():
        value = cfg
        for part in path:
            value = getattr(value, part)
        if value:
            pending.append(env_name)
    if any(provider.api_key for provider in cfg.llm.providers):
        pending.append(PROVIDER_API_KEYS_ENV)
    return pending


def provider_runtime_env(providers: list[LLMProviderConfig]) -> dict[str, str]:
    """Only the two hub backups; keep credentials in the existing secret env."""
    backups = providers[:2]
    metadata = [{"id": p.id, "name": p.name, "base_url": p.base_url,
                 "models": p.models, "protocol": p.protocol,
                 "context_windows": p.context_windows} for p in backups]
    return {
        PROVIDERS_ENV: json.dumps(metadata, ensure_ascii=False),
        PROVIDER_API_KEYS_ENV: json.dumps({p.id: p.api_key for p in backups}, ensure_ascii=False),
    }


def _providers_from_env(raw: str) -> list[LLMProviderConfig]:
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError
        providers = []
        seen = {"default"}
        allowed = {"id", "name", "base_url", "models", "protocol", "context_windows"}
        for item in items:
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError
            if any(not isinstance(item.get(key), str) or not item[key].strip()
                   for key in ("id", "base_url")):
                raise ValueError
            if item["id"] in seen or not isinstance(item.get("name", ""), str):
                raise ValueError
            models = item.get("models")
            if not isinstance(models, list) or not models or any(
                    not isinstance(model, str) or not model.strip() for model in models):
                raise ValueError
            if item.get("protocol", "openai") not in {"openai", "responses", "anthropic"}:
                raise ValueError
            windows = item.get("context_windows", {})
            if not isinstance(windows, dict):
                raise ValueError
            provider = LLMProviderConfig(**item)
            provider.context_windows = _normalize_context_windows(windows)
            providers.append(provider)
            seen.add(provider.id)
        return providers
    except (ValueError, TypeError):
        raise ValueError(f"环境变量 {PROVIDERS_ENV} 格式无效，请重新同步云端模型设置") from None


def provider_api_key_env_overrides() -> dict[str, str]:
    global _PROVIDER_ENV_WARNING, _PROVIDER_ENV_WARNED_RAW
    raw = os.getenv(PROVIDER_API_KEYS_ENV, "").strip()
    if not raw:
        _PROVIDER_ENV_WARNING = ""
        _PROVIDER_ENV_WARNED_RAW = None
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        values = None
    if not isinstance(values, dict):
        _PROVIDER_ENV_WARNING = (
            f"环境变量 {PROVIDER_API_KEYS_ENV} 格式无效，已忽略；"
            '应填写 JSON 对象，例如 {"provider-id":"key"}。'
        )
        if raw != _PROVIDER_ENV_WARNED_RAW:
            _LOGGER.warning(_PROVIDER_ENV_WARNING)
            _PROVIDER_ENV_WARNED_RAW = raw
        return {}
    _PROVIDER_ENV_WARNING = ""
    _PROVIDER_ENV_WARNED_RAW = None
    return {
        str(provider_id): str(api_key)
        for provider_id, api_key in values.items()
        if str(provider_id) and isinstance(api_key, str) and api_key
    }


def provider_api_key_env_warning() -> str:
    return _PROVIDER_ENV_WARNING


def subprocess_env_without_secrets() -> dict[str, str]:
    """Environment for model-controlled commands, excluding XiaoA credentials."""
    blocked = {name.upper() for name in (*SECRET_ENV_FIELDS, PROVIDER_API_KEYS_ENV)}
    return {name: value for name, value in os.environ.items() if name.upper() not in blocked}


# 允许用环境变量覆盖关键项(方便部署时不改文件)
def apply_env_overrides(cfg: Config) -> Config:
    if not cfg.llm.settings_saved:
        if v := os.getenv("AGENT_LLM_PROTOCOL"):
            protocol = v.strip().lower()
            cfg.llm.protocol = protocol if protocol in {"openai", "anthropic", "responses"} else "openai"
        if v := os.getenv("AGENT_LLM_BASE_URL"):
            cfg.llm.base_url = v
        if v := os.getenv("AGENT_CHAT_MODEL"):
            cfg.llm.chat_model = v
        if v := os.getenv("AGENT_MODEL_OPTIONS"):
            cfg.llm.models = [model.strip() for model in v.split(",") if model.strip()]
        if v := os.getenv("AGENT_LLM_PROVIDER_NAME"):
            cfg.llm.provider_name = v
        if v := os.getenv("AGENT_LLM_CONTEXT_WINDOWS"):
            cfg.llm.context_windows = _normalize_context_windows(json.loads(v))
        if v := os.getenv("AGENT_CONTEXT_WINDOW"):
            window = int(v)
            if not 4096 <= window <= 2_000_000:
                raise ValueError("AGENT_CONTEXT_WINDOW 必须为 4096 到 2000000")
            cfg.agent.context_window = window
    if v := os.getenv("AGENT_LLM_API_KEY"):
        cfg.llm.api_key = v
    cfg.llm.models = list(dict.fromkeys(
        model for model in [cfg.llm.chat_model, *cfg.llm.models] if model
    ))
    cfg.llm.vision_model = cfg.llm.chat_model
    if v := os.getenv("AGENT_MEMORY_BACKUP_DIR"):
        cfg.memory.backup_dir = v
    if v := os.getenv("AGENT_CLOUD_BACKUP_ACCESS_KEY"):
        cfg.memory.cloud_backup.access_key = v
    if v := os.getenv("AGENT_CLOUD_BACKUP_SECRET_KEY"):
        cfg.memory.cloud_backup.secret_key = v
    if v := os.getenv("AGENT_TENCENT_SECRET_ID"):
        cfg.voice.secret_id = _normalize_tencent_credential(v, "SecretId")
    if v := os.getenv("AGENT_TENCENT_SECRET_KEY"):
        cfg.voice.secret_key = _normalize_tencent_credential(v, "SecretKey")
    if v := os.getenv("AGENT_TENCENT_REGION"):
        cfg.voice.region = v
    if v := os.getenv("AGENT_TTS_VOICE"):
        try:
            cfg.voice.tts_voice = int(v)
        except ValueError:
            _LOGGER.warning("忽略无效的 AGENT_TTS_VOICE:应为整数")
    if v := os.getenv("AGENT_TTS_SPEED"):
        try:
            cfg.voice.tts_speed = int(v)
        except ValueError:
            _LOGGER.warning("忽略无效的 AGENT_TTS_SPEED:应为整数")
    if cfg.voice.secret_id and cfg.voice.secret_key and not cfg.voice.provider:
        cfg.voice.provider = "tencent"
    if v := os.getenv("AGENT_FEISHU_APP_ID"):
        cfg.feishu.app_id = v
    if v := os.getenv("AGENT_FEISHU_APP_SECRET"):
        cfg.feishu.app_secret = v
    elif not cfg.feishu.app_secret:
        try:
            from backend.core.vault import get
            cfg.feishu.app_secret = get("feishu_app_secret")
        except Exception:
            pass
    if PROVIDERS_ENV in os.environ and not cfg.llm.settings_saved:
        cfg.llm.providers = _providers_from_env(os.environ[PROVIDERS_ENV])
    provider_keys = provider_api_key_env_overrides()
    for provider in cfg.llm.providers:
        if provider.id in provider_keys:
            provider.api_key = provider_keys[provider.id]
    # 角色相关(部署时用环境变量最方便:云端 AGENT_ROLE=hub,电脑 AGENT_ROLE=worker)
    if v := os.getenv("AGENT_ROLE"):
        cfg.role.mode = v
    if v := os.getenv("AGENT_HUB_URL"):
        cfg.role.hub_url = v
    if v := os.getenv("AGENT_HUB_TOKEN"):
        cfg.role.hub_token = v
    if v := os.getenv("AGENT_WORKER_ID"):
        cfg.role.worker_id = v
    if v := os.getenv("AGENT_HUB_FINGERPRINT"):
        cfg.role.tls_fingerprint = v
    if not cfg.role.worker_id:
        cfg.role.worker_id = platform.node() or "windows-worker"
    v = os.getenv("AGENT_WEB_PASSWORD")
    if v or (cfg.role.is_hub and v is not None):
        cfg.auth.password = v
        cfg.auth.password_env_managed = True
    if v := os.getenv("AGENT_SERVER_HOST"):
        cfg.server.host = v
    if v := os.getenv("AGENT_SERVER_PORT"):
        cfg.server.port = int(v)
    if v := os.getenv("AGENT_FEISHU_ALLOWED_OPEN_IDS"):
        cfg.feishu.allowed_open_ids = [x.strip() for x in v.split(",") if x.strip()]
    return cfg
