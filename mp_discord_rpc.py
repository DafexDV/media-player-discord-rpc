import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import override

import dbus
import pypresence.exceptions as pyexp
from pydantic import BaseModel
from pypresence.presence import Presence
from pypresence.types import ActivityType


class Config(BaseModel):
    application_id: str
    elisa_asset_name: str


class ConfigSerializer(ABC):
    @abstractmethod
    def load(self, path: str) -> Config:
        pass

    @abstractmethod
    def save(self, path: str, config: Config):
        pass

    def _verify_path(self, path: str):
        p = Path(path)

        if not p.is_file() or p.stat().st_size > 1024:
            raise ValueError("Configuration file is too large")


class JsonConfigSerializer(ConfigSerializer):
    @override
    def load(self, path: str) -> Config:
        self._verify_path(path)

        with open(path, "rb") as f:
            raw = json.load(f)
            return Config.model_validate(raw)

    @override
    def save(self, path: str, config: Config):
        self._verify_path(path)

        with open(path, "w") as f:
            raw = config.model_dump_json(indent=4)
            f.write(raw)


CONFIG_DIR = Path.home() / ".config" / "mp_discord_rpc"
CONFIG_FILE = CONFIG_DIR / "config.json"
SHARE_DIR = Path.home() / ".local" / "share" / "mp_discord_rpc"
LOGGING_FILE = SHARE_DIR / "app.log"
DEFAULT_CONFIG = Config(
    # By default, the application id will be an application
    # that I, dafexdv, created.
    # Therefore, by default, you are depending on my Discord account for the script to work.
    # Feel free to configure your own discord application and
    # passing its id to the script config file
    # ~/.config/mp_discord_rpc/config.json
    application_id="1544121036658704464",
    elisa_asset_name="elisa",
)


class Application:
    # Dependencies
    config_serializer: ConfigSerializer

    # State
    config: Config
    logger: logging.Logger

    def __init__(self, config_serializer: ConfigSerializer) -> None:
        self.config_serializer = config_serializer

        self._setup_logger()
        self._load_config()

    def _setup_logger(self):
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        LOGGING_FILE.touch(exist_ok=True)

        lf = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # File handler
        fh = logging.FileHandler(LOGGING_FILE, mode="w")
        fh.setFormatter(lf)
        fh.setLevel(logging.DEBUG)

        # Console handler
        ch = logging.StreamHandler()
        ch.setFormatter(lf)
        ch.setLevel(logging.DEBUG)

        self.logger = logging.getLogger("mp_discord_rpc")
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    def _load_config(self):
        self.logger.debug("Loading application config")

        if not CONFIG_DIR.exists():
            CONFIG_DIR.mkdir(parents=True)
            self.logger.debug("Created config dir: %s", CONFIG_DIR)

        if not CONFIG_FILE.exists():
            CONFIG_FILE.touch()
            self.config_serializer.save(CONFIG_FILE.__str__(), DEFAULT_CONFIG)
            self.logger.debug(
                "Created config file with default config: %s %s",
                CONFIG_FILE,
                DEFAULT_CONFIG,
            )

        self.config = self.config_serializer.load(CONFIG_FILE.__str__())
        self.logger.info("Loaded application config from: %s", CONFIG_FILE)

    def run(self):
        presence = Presence(self.config.application_id)

        # First loop: try to connect to discord
        while True:
            self.logger.debug("Connecting to discord...")
            try:
                presence.connect()
            except (FileNotFoundError, ConnectionRefusedError):
                self.logger.error(
                    "Failed to connect to RPC. Do you have discord running?"
                )
                time.sleep(5)
            else:
                self.logger.info(
                    "Connected to discord successfully (application_id=(%s)",
                    presence.client_id,
                )
                break

        bus = dbus.SessionBus()
        already_stopped = True

        # Second loop: get elisa dbus data
        while True:
            self.logger.debug("Getting data from Elisa...")
            try:
                track_name, track_artist, playback_status = (
                    self._extract_data_from_elisa(bus)
                )
            except dbus.DBusException:
                self.logger.error(
                    "Failed to get elisa information. Do you have Elisa running?"
                )
                continue

            # Update discord rpc activity
            if playback_status == "Playing":
                already_stopped = False
                presence.update(
                    activity_type=ActivityType.LISTENING,
                    large_image="elisa",
                    details=track_name,
                    state=track_artist,
                )
                time.sleep(5)
            elif playback_status == "Stopped":
                if not already_stopped:
                    presence.update(
                        activity_type=ActivityType.LISTENING,
                        large_image="elisa",
                        details=track_name,
                        state=track_artist,
                    )
                    already_stopped = True
                time.sleep(5)
                while True:
                    try:
                        presence.clear()
                    except (ConnectionRefusedError, pyexp.InvalidID):
                        self.logger.error("Connection reset, retrying connection")
                        # This handles error just in case Discord still hasn't turned on
                        while True:
                            try:
                                presence.connect()
                            except (FileNotFoundError, ConnectionRefusedError):
                                self.logger.error(
                                    "Could not connect to RPC. Do you have discord running?"
                                )
                                time.sleep(5)
                            else:
                                break
                        time.sleep(5)
                    else:
                        break
            else:
                while True:
                    try:
                        presence.clear()
                    except (ConnectionRefusedError, pyexp.InvalidID):
                        self.logger.error("Connection reset, retrying connection")
                        # This handles error just in case Discord still hasn't turned on
                        while True:
                            try:
                                presence.connect()
                            except (FileNotFoundError, ConnectionRefusedError):
                                self.logger.error(
                                    "Could not connect to RPC. Do you have discord running?"
                                )
                                time.sleep(5)
                            else:
                                break
                        time.sleep(5)
                    else:
                        break
                time.sleep(5)

    def _extract_data_from_elisa(self, bus: dbus.SessionBus) -> tuple[str, str, str]:
        proxy = bus.get_object(
            "org.mpris.MediaPlayer2.elisa", "/org/mpris/MediaPlayer2"
        )

        props = dbus.Interface(proxy, dbus_interface="org.freedesktop.DBus.Properties")

        metadata: dbus.Dictionary = props.Get(
            "org.mpris.MediaPlayer2.Player", "Metadata"
        )
        playback_status: dbus.String = props.Get(
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus"
        )

        # Extract metadata
        track_name: dbus.String = metadata.get("xesam:title") or metadata.get(
            "xesam:url"
        )
        track_artists: dbus.Array = metadata.get("xesam:artist")

        # Compact the lists of artists to one string
        track_artist = None
        if track_artists:
            track_artist = ", ".join(track_artists)

        return (str(track_name), str(track_artist), str(playback_status))


def main():
    config_serializer = JsonConfigSerializer()
    app = Application(config_serializer)

    app.run()


if __name__ == "__main__":
    main()
