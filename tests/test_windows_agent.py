from pathlib import Path


def test_windows_agent_is_read_only_and_uses_the_existing_transport_contract():
    script = Path("agent/windows/pyfog-agent.ps1").read_text(encoding="utf-8")

    assert script.startswith("<#")
    assert "#>" in script
    assert "Get-CimInstance" in script
    assert "Invoke-RestMethod" in script
    assert "PYFOG_INVENTORY_TOKEN" in script
    assert "-SkipCertificateCheck" not in script
    for dangerous in ("Clear-Disk", "Format-Volume", "Initialize-Disk", "diskpart"):
        assert dangerous not in script
    assert 'Authorization = "Bearer $token"' in script
