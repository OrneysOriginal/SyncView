from src.media.metadata import MediaInfo


def media_matches(left: MediaInfo, right: MediaInfo) -> bool:
    return (
        left.file_size == right.file_size
        and abs(left.duration_ms - right.duration_ms) <= 1000
        and left.fingerprint == right.fingerprint
    )
