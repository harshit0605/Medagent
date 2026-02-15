from services.whatsapp_gateway.main import MAX_LOG_ENTRIES, _append_log


def test_message_log_is_bounded():
    for i in range(MAX_LOG_ENTRIES + 25):
        _append_log({"i": i})

    # We only assert bounded behavior; contents are managed by FIFO trimming.
    from services.whatsapp_gateway.main import MESSAGE_LOG

    assert len(MESSAGE_LOG) == MAX_LOG_ENTRIES
