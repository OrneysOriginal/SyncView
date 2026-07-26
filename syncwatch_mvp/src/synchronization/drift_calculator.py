def calculate_drift_ms(expected_position_ms: int, remote_position_ms: int) -> int:
    return int(remote_position_ms - expected_position_ms)


def needs_correction(drift_ms: int) -> bool:
    return abs(drift_ms) > 400
