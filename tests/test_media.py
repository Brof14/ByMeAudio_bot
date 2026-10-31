from types import SimpleNamespace

from bot import detect_media


def make_message(document=None, **kwargs):
    return SimpleNamespace(
        voice=kwargs.get("voice"),
        video_note=kwargs.get("video_note"),
        audio=kwargs.get("audio"),
        video=kwargs.get("video"),
        document=document,
    )


def test_recognizes_document_extension_without_mime():
    document = SimpleNamespace(mime_type="application/octet-stream", file_name="VOICE.MP3")
    found = detect_media(make_message(document))
    assert found is not None
    assert found[1] == "audio"


def test_recognizes_video_document_extension_without_mime():
    document = SimpleNamespace(mime_type=None, file_name="clip.mkv")
    found = detect_media(make_message(document))
    assert found is not None
    assert found[1] == "video"


def test_ignores_unrelated_documents():
    document = SimpleNamespace(mime_type="application/pdf", file_name="report.pdf")
    assert detect_media(make_message(document)) is None


def test_video_note_classification():
    note = SimpleNamespace(duration=10)
    assert detect_media(make_message(video_note=note))[1] == "video_note"
