# AGENTS.md

**Rule:** In each command, **define → use**. Do **not** escape `$`. Use generic `'path/to/file.ext'`.

---

## 0) UI RULES — read `docs/design/style-guide.md` first

Any change that touches how the app **looks** (`frontend/css/common.css`,
`frontend/pages/app.html`, or any new UI) MUST follow
[docs/design/style-guide.md](docs/design/style-guide.md). It is the single source of truth for
color, typography, spacing, radii, icons, motion, and components — the art style must stay
consistent across the whole app.

- Read the relevant section **before** writing UI code.
- Use the `common.css` tokens; never write raw HEX / raw `rgba()` (see §2-3 for the 3 exceptions).
- Font sizes 11 / 12 / 13 / 14; radii, spacing and fixed heights come from the tables in §4–§5.
- Lucide icons only, no emoji; stroke-width per §7. Call `lucide.createIcons()` after dynamic DOM.
- Reuse existing components (`.btn-ghost`, `.file-menu`, badges, status dots) — do not invent
  near-duplicates.
- Run the checklist in **§14** before finishing.
- If a genuinely new pattern is needed, **update style-guide.md first**, then implement it.

Also keep `docs/` up to date after each unit of work (see `CLAUDE.md`).

---

## 1) READ (UTF‑8 no BOM, line‑numbered)

```bash
bash -lc 'powershell -NoLogo -Command "
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false);
Set-Location -LiteralPath (Convert-Path .);
function Get-Lines { param([string]$Path,[int]$Skip=0,[int]$First=40)
  $enc=[Text.UTF8Encoding]::new($false)
  $text=[IO.File]::ReadAllText($Path,$enc)
  if($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF){ $text=$text.Substring(1) }
  $ls=$text -split \"`r?`n\"
  for($i=$Skip; $i -lt [Math]::Min($Skip+$First,$ls.Length); $i++){ \"{0:D4}: {1}\" -f ($i+1), $ls[$i] }
}
Get-Lines -Path \"path/to/file.ext\" -First 120 -Skip 0
"'
```

---

## 2) WRITE (UTF‑8 no BOM, atomic replace, backup)

```bash
bash -lc 'powershell -NoLogo -Command "
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false);
Set-Location -LiteralPath (Convert-Path .);
function Write-Utf8NoBom { param([string]$Path,[string]$Content)
  $dir = Split-Path -Parent $Path
  if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  $tmp = [IO.Path]::GetTempFileName()
  try {
    $enc = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($tmp,$Content,$enc)
    Move-Item $tmp $Path -Force
  }
  finally {
    if (Test-Path $tmp) {
      Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
  }
}
$file = "path/to/your_file.ext"
$enc  = [Text.UTF8Encoding]::new($false)
$old  = (Test-Path $file) ? ([IO.File]::ReadAllText($file,$enc)) : ''
Write-Utf8NoBom -Path $file -Content ($old+"`nYOUR_TEXT_HERE`n")
"'
```