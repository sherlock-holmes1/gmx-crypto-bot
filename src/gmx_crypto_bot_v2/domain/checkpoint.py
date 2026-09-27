"""Opening-order normalization shared by capture and replay."""

from __future__ import annotations


def normalize_recorded_order_checkpoint(values: dict[str, Any]) -> dict[str, Any]:
    """Correct flags from recordings captured with the old 12-slot decoder.

    That decoder read the extra numeric slot as isLong and the data-list
    pointer as an array length. Its synthetic dataList contains the tuple head,
    including the four actual flags at indices 13 through 16.
    """
    result = dict(values)
    words = result.get("dataList")
    if not isinstance(words, list) or len(words) < 18:
        return result
    try:
        if int(words[0], 16) != result.get("orderType") or int(words[17], 16) < 19 * 32:
            return result
        flags = [int(word, 16) for word in words[13:17]]
        if any(flag not in (0, 1) for flag in flags):
            return result
    except (TypeError, ValueError):
        return result
    for name, flag in zip(
        ("isLong", "shouldUnwrapNativeToken", "isFrozen", "autoCancel"),
        flags,
        strict=True,
    ):
        result[name] = bool(flag)
    result["legacy_decoder_words"] = result.pop("dataList")
    result["data_list_unavailable"] = True
    return result


from typing import Any
