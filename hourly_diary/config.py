"""Configuration: explicit owner, no first-user registration or secret logging."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    owner_id: int
    data_dir: Path = Path('/var/lib/hourly-diary')
    timezone: str = 'Europe/Moscow'
    chat_id: int | None = None
    stt_provider: str = 'none'
    stt_api_key: str = field(default='', repr=False)
    stt_model: str = ''
    stt_endpoint: str = ''
    max_audio_bytes: int = 20 * 1024 * 1024
    storage_limit_bytes: int = 500 * 1024 * 1024
    max_export_bytes: int = 45 * 1024 * 1024
    max_export_days: int = 31

    def __post_init__(self):
        if self.owner_id <= 0 or (self.chat_id is not None and self.chat_id != self.owner_id):
            raise ValueError('OWNER_ID must be positive; CHAT_ID must be the owner private chat')
        ZoneInfo(self.timezone)
        if self.stt_provider not in ('none', 'groq', 'openai'):
            raise ValueError('STT_PROVIDER must be none, groq or openai')
        if any(x <= 0 for x in (self.max_audio_bytes, self.storage_limit_bytes,
                               self.max_export_bytes, self.max_export_days)):
            raise ValueError('Limits must be positive')
        if self.stt_endpoint and not self.stt_endpoint.startswith('https://'):
            raise ValueError('STT endpoint must use HTTPS')

    @property
    def db_path(self):
        return self.data_dir / 'diary.sqlite3'

    @property
    def endpoint(self):
        return self.stt_endpoint or {
            'groq': 'https://api.groq.com/openai/v1/audio/transcriptions',
            'openai': 'https://api.openai.com/v1/audio/transcriptions',
        }.get(self.stt_provider, '')

    @property
    def model(self):
        return self.stt_model or {'groq': 'whisper-large-v3-turbo', 'openai': 'whisper-1'}.get(self.stt_provider, '')

    @classmethod
    def from_env(cls):
        # dotenv deliberately not loaded by application. systemd EnvironmentFile
        # or an explicitly prepared shell provides values; never inspect archives.
        return cls(bot_token=os.environ.get('BOT_TOKEN', ''), owner_id=int(os.environ['OWNER_ID']),
                   data_dir=Path(os.environ.get('DATA_DIR', '/var/lib/hourly-diary')),
                   timezone=os.environ.get('TIMEZONE', 'Europe/Moscow'),
                   chat_id=int(os.environ['CHAT_ID']) if os.environ.get('CHAT_ID') else None,
                   stt_provider=os.environ.get('STT_PROVIDER', 'none').lower(),
                   stt_api_key=os.environ.get('STT_API_KEY', ''),
                   stt_model=os.environ.get('STT_MODEL', ''), stt_endpoint=os.environ.get('STT_ENDPOINT', ''),
                   max_audio_bytes=int(os.environ.get('MAX_AUDIO_BYTES', 20 * 1024 * 1024)),
                   storage_limit_bytes=int(os.environ.get('STORAGE_LIMIT_BYTES', 500 * 1024 * 1024)),
                   max_export_bytes=int(os.environ.get('MAX_EXPORT_BYTES', 45 * 1024 * 1024)),
                   max_export_days=int(os.environ.get('MAX_EXPORT_DAYS', 31)))
