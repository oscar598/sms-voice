import os

import envfile


def test_parse_handles_comments_quotes_and_export():
    text = "# comment\n\nANTHROPIC_API_KEY=abc\nexport REFERRAL_TOKEN='t0k'\nX=\"q\"\nnot a pair\n"
    assert envfile.parse(text) == {"ANTHROPIC_API_KEY": "abc", "REFERRAL_TOKEN": "t0k", "X": "q"}


def test_load_never_overrides_and_skips_empty(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("A_KEY=from_file\nB_KEY=from_file\nEMPTY_KEY=\n")
    monkeypatch.setenv("A_KEY", "from_shell")
    monkeypatch.delenv("B_KEY", raising=False)
    monkeypatch.delenv("EMPTY_KEY", raising=False)
    assert envfile.load(f) == {"B_KEY": "from_file"}
    assert os.environ["A_KEY"] == "from_shell" and "EMPTY_KEY" not in os.environ
    monkeypatch.delenv("B_KEY")


def test_missing_file_is_fine(tmp_path):
    assert envfile.load(tmp_path / "nope") == {}
