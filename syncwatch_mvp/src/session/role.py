from enum import Enum


class Role(str, Enum):
    NONE = "none"
    HOST = "host"
    CLIENT = "client"
