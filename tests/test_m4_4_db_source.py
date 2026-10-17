def test_draw_source_is_persisted(tmp_path, monkeypatch):
    # V10-00b：全局路径用 monkeypatch 修改（用例结束自动还原），并重置 db 连接缓存
    monkeypatch.setenv("LOTT_HOME", str(tmp_path))
    monkeypatch.setenv("LOTT_DB", str(tmp_path / "test.db"))
    from lottery import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.close()
    db.upsert_draws([{"issue":"2026001", "date":"2026-01-01", "reds":[1,2,3,4,5,6], "blue":7,
                      "order":[1,2,3,4,5,6], "source":"backup"}])
    assert db.load_draws()[0]["source"] == "backup"
