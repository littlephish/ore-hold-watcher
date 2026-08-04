# MSIX packaging

The [MSIX workflow](../.github/workflows/msix.yml) builds a signed `.msix`
installer from the same version tags as the exe release and attaches it (plus
the public `.cer`) to that GitHub Release.

- **Build:** Nuitka `--standalone` (a folder of files, not onefile — the right
  shape for a package).
- **Manifest:** [`AppxManifest.xml`](AppxManifest.xml), a packaged full-trust
  Win32 app (`runFullTrust`). Two tokens are filled in at build time:
  `{{VERSION}}` from the tag, `{{PUBLISHER}}` from the signing cert's subject.
- **Package/sign:** `makeappx pack` then `signtool sign` from the Windows SDK
  on the runner.

## Signing certificate (do this once for real releases)

MSIX will not install unless it's signed, and the manifest `Publisher` must
match the signing cert exactly. For upgrades to work, that identity has to stay
**stable across releases**, so store one cert as repo secrets:

```powershell
# 1. Create a code-signing cert (self-signed is fine for sideloading).
#    Use a subject you'll keep forever.
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
  -Subject "CN=Jared Armstrong" `
  -KeyUsage DigitalSignature `
  -CertStoreLocation "Cert:\CurrentUser\My" `
  -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")

# 2. Export it to a password-protected .pfx
$pw = ConvertTo-SecureString "a-strong-password" -AsPlainText -Force
Export-PfxCertificate -Cert $cert -FilePath sign.pfx -Password $pw

# 3. Base64 the .pfx for the secret
[Convert]::ToBase64String([IO.File]::ReadAllBytes("sign.pfx")) | Set-Clipboard
```

Then in **Settings → Secrets and variables → Actions** add:

- `MSIX_PFX_BASE64` — the base64 string from step 3
- `MSIX_PFX_PASSWORD` — the password from step 2

If both secrets are set, `AppxManifest.xml`'s `Publisher` (`CN=Jared Armstrong`
above) is picked up from the cert automatically — keep the manifest's
`PublisherDisplayName`/`Identity Name` stable to preserve the package identity.

**Without the secrets** the workflow falls back to an *ephemeral* self-signed
cert. The package still builds and installs (after trusting the `.cer`), but
each build has a different identity, so it can't upgrade an earlier install —
use it only for testing the pipeline.

For a cert with no warnings on other machines, use a real code-signing
certificate from a CA instead of self-signed; the secret setup is identical.

## Installing (end users)

The `.msix` is signed by a self-signed cert unless you bought a CA cert, so the
public cert has to be trusted first:

1. Download `OreHoldWatcher.cer` and the `.msix` from the release.
2. Right-click the `.cer` → **Install Certificate** → **Local Machine** →
   place it in **Trusted People** (or **Trusted Root Certification
   Authorities**). Admin rights required.
3. Double-click the `.msix` and choose **Install** (or
   `Add-AppxPackage .\OreHoldWatcher-x.y.z-x64.msix`).

To remove: **Settings → Apps**, or `Get-AppxPackage *OreHoldWatcher* |
Remove-AppxPackage`.

## Notes

- **Updates:** the app's in-app self-updater is disabled when it detects it's
  running packaged (`is_packaged()` in `app.py`), because the install dir is
  read-only. Update by installing a newer `.msix` (same publisher identity).
  A future `.appinstaller` could automate this from a stable URL.
- **Config location:** packaged, the exe folder isn't writable, so settings and
  ledger fall back to `%APPDATA%\OreHoldWatcher` (handled by `config_dir()`).
- The plain onefile `.exe` release (`release.yml`) is unaffected and remains the
  primary distribution; MSIX is an alternative install format.
