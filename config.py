"""
Configuration layer for agy-router.
Supports ~/.agy-router/config.json with environment variable overrides.
"""
import os
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.agy-router")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.json")

@dataclass
class RouterConfig:
    """Configuration settings for agy-router."""
    default_key_index: int = 1
    cooldown_seconds: int = 60
    max_total_sync_bytes: int = 2 * 1024 * 1024  # 2 MB
    max_file_size_bytes: int = 512 * 1024  # 512 KB
    log_level: str = "INFO"
    git_auto_checkpoint: bool = True
    default_session_timeout: int = 3600  # 1 hour in seconds
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RouterConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})

def load_config(config_path: Optional[str] = None) -> RouterConfig:
    """
    Load configuration from file with environment variable overrides.
    Priority: CLI args > Env vars > Config file > Defaults
    """
    config_file = config_path or DEFAULT_CONFIG_PATH
    config = RouterConfig()
    
    # 1. Load from config file if exists
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                file_data = json.load(f)
            config = RouterConfig.from_dict(file_data)
        except (json.JSONDecodeError, OSError, TypeError):
            # Ignore corrupted config, use defaults
            pass
    
    # 2. Apply environment variable overrides
    env_overrides = {
        'AGY_DEFAULT_KEY_INDEX': ('default_key_index', int),
        'AGY_COOLDOWN_SECONDS': ('cooldown_seconds', int),
        'AGY_MAX_TOTAL_SYNC_BYTES': ('max_total_sync_bytes', int),
        'AGY_MAX_FILE_SIZE_BYTES': ('max_file_size_bytes', int),
        'AGY_LOG_LEVEL': ('log_level', str),
        'AGY_GIT_AUTO_CHECKPOINT': ('git_auto_checkpoint', lambda x: x.lower() in ('true', '1', 'yes')),
        'AGY_DEFAULT_SESSION_TIMEOUT': ('default_session_timeout', int),
    }
    
    for env_var, (attr, converter) in env_overrides.items():
        if env_var in os.environ:
            try:
                setattr(config, attr, converter(os.environ[env_var]))
            except (ValueError, TypeError):
                pass  # Ignore invalid env values
    
    return config

def save_config(config: RouterConfig, config_path: Optional[str] = None) -> bool:
    """Save configuration to file."""
    config_file = config_path or DEFAULT_CONFIG_PATH
    try:
        os.makedirs(os.path.dirname(config_file), mode=0o700, exist_ok=True)
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
        os.chmod(config_file, 0o600)
        return True
    except Exception:
        return False

def get_config_path(config_path: Optional[str] = None) -> str:
    """Get the effective config path."""
    return config_path or DEFAULT_CONFIG_PATH