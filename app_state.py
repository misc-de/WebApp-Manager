from __future__ import annotations

from dataclasses import dataclass

from webapp_constants import ADDRESS_KEY, ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY, USER_AGENT_NAME_KEY, USER_AGENT_VALUE_KEY


@dataclass
class WebAppState:
    title: str
    address: str
    engine_id: str
    active: bool
    user_agent_name: str
    user_agent_value: str
    icon_path: str
    profile_name: str
    profile_path: str

    @classmethod
    def from_entry_and_options(cls, entry, options: dict[str, str]):
        return cls(
            title=entry.title,
            address=options.get(ADDRESS_KEY, ''),
            engine_id=options.get('EngineID', ''),
            active=bool(entry.active),
            user_agent_name=options.get(USER_AGENT_NAME_KEY, ''),
            user_agent_value=options.get(USER_AGENT_VALUE_KEY, ''),
            icon_path=options.get(ICON_PATH_KEY, ''),
            profile_name=options.get(PROFILE_NAME_KEY, ''),
            profile_path=options.get(PROFILE_PATH_KEY, ''),
        )

    @classmethod
    def from_file_data(cls, file_data: dict, fallback: 'WebAppState | None' = None):
        fallback = fallback or cls('', '', '', True, '', '', '', '', '')
        file_engine_id = file_data.get('engine_id')
        return cls(
            title=file_data.get('title', '') or fallback.title,
            address=file_data.get('address') or fallback.address,
            engine_id=fallback.engine_id if file_engine_id is None else str(file_engine_id),
            active=bool(file_data.get('active', True)),
            user_agent_name=file_data.get('user_agent_name') or fallback.user_agent_name,
            user_agent_value=file_data.get('user_agent_value') or fallback.user_agent_value,
            icon_path=file_data.get('icon_path') or fallback.icon_path,
            profile_name=file_data.get('profile_name') or fallback.profile_name,
            profile_path=file_data.get('profile_path') or fallback.profile_path,
        )
