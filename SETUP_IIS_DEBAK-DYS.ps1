# DEBAK-DYS IIS kurulum (yeni site; eski "DebakDYS" sitesine DOKUNMAZ)
# Calistirma: SUNUCUDA, YONETICI PowerShell. Sifre / anahtar bu dosyaya YAZILMAZ (.env sunucuda elle olusturulur).
#
#   .\SETUP_IIS_DEBAK-DYS.ps1 -KuruCalisma                 # ne yapilacagini listeler, hicbir sey degistirmez
#   .\SETUP_IIS_DEBAK-DYS.ps1                              # kurar (varsayilan: site DEBAK-DYS, port 8082, ApplicationPoolIdentity)
#   .\SETUP_IIS_DEBAK-DYS.ps1 -Port 8090 -IISKur           # Web-Server rolu eksikse kurar (yalniz bu bilgisayarda yonetici onayiyla)
#   .\SETUP_IIS_DEBAK-DYS.ps1 -HavuzLocalSystem            # havuz kimligi LocalSystem (UNC / Office onizleme sorununda; guvenlik riski yuksek)
#
# Uygulama AYRI klasore kopyalanir (varsayilan C:\inetpub\DEBAK-DYS-App); betigin bulundugu klasor kaynak olur.
#   .\SETUP_IIS_DEBAK-DYS.ps1 -Kaynak "C:\inetpub\wwwroot\DEBAK-DYS" -Kok "C:\inetpub\DEBAK-DYS-App" -Port 8088
# Kopyalama /MIR DEGILDIR: Kok'teki .env, .venv, logs, uploads, temp silinmez; tekrar calistirmak guvenlidir (guncelleme).
#
# On kosullar: HttpPlatformHandler 1.2 (MSI), Python 3.10+ (64-bit) veya script otomatik kurar, ODBC Driver 17 for SQL Server.
param(
    # Kok = uygulamanin CALISACAGI ayri klasor (veri / arsiv klasorleri ile karismaz). Kaynak = tam uygulama dosyalarinin bulundugu klasor.
    [string]$Kok = "C:\inetpub\DEBAK-DYS-App",
    [string]$Kaynak = $PSScriptRoot,
    [string]$Site = "DEBAK-DYS",
    [string]$Havuz = "DEBAK-DYS-Pool",
    [int]$Port = 8082,
    [string]$HttpPlatformMsi = "",
    [switch]$IISKur,
    [switch]$HavuzLocalSystem,
    [switch]$KuruCalisma
)

$ErrorActionPreference = "Stop"
function Adim([string]$m) { Write-Host ("=== " + $m + " ===") -ForegroundColor Cyan }
function Yap([string]$aciklama, [scriptblock]$is) {
    if ($KuruCalisma) { Write-Host ("[KURU] " + $aciklama) -ForegroundColor DarkYellow; return }
    Write-Host ("- " + $aciklama)
    & $is
}

# yonetici mi?
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Yonetici PowerShell ile calistirin."
}

if (-not $Kaynak -or -not (Test-Path (Join-Path $Kaynak "app.py"))) { throw "Kaynak klasorde app.py yok: '$Kaynak'. -Kaynak <tam uygulama klasoru> verin (bkz. GECIS_GUNU.md adim 3)." }
$Kaynak = (Resolve-Path $Kaynak).Path.TrimEnd('')
$Kok = $Kok.TrimEnd('')
if ($Kok -ieq $Kaynak) { Write-Warning "Kok ile Kaynak ayni klasor; uygulama ayri klasore tasinmiyor." }
elseif ($Kok.StartsWith($Kaynak + "\", [StringComparison]::OrdinalIgnoreCase) -or $Kaynak.StartsWith($Kok + "\", [StringComparison]::OrdinalIgnoreCase)) { throw "Kok ve Kaynak ic ice olamaz: $Kok <-> $Kaynak" }
Yap "uygulama dosyalari kopyalaniyor: $Kaynak -> $Kok (.env, .venv, logs, uploads, temp, *.db haric; silme yok)" {
    if ($Kok -ine $Kaynak) {
        New-Item -ItemType Directory -Force -Path $Kok | Out-Null
        robocopy $Kaynak $Kok /E /XD .venv logs uploads temp __pycache__ _test .git /XF .env *.db *.pyc "_*.txt" pytest_out.txt /NFL /NDL /NP /R:1 /W:1 | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy hata verdi (kod $LASTEXITCODE)" }
    }
}
if (-not $KuruCalisma) { Set-Location $Kok }
Write-Host "Kok: $Kok | Site: $Site | Havuz: $Havuz | Port: $Port"

function Refresh-ProcessPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

# ------------------------------------------------------------ 0) IIS rolu
Adim "0) IIS"
$iisVar = [bool](Get-Module -ListAvailable -Name WebAdministration)
if (-not $iisVar) {
    if ($IISKur) {
        Yap "Web-Server rolu kuruluyor" {
            if (Get-Command Install-WindowsFeature -ErrorAction SilentlyContinue) {
                Install-WindowsFeature -Name Web-Server, Web-Static-Content, Web-Default-Doc, Web-Http-Errors, Web-Http-Logging, Web-Request-Monitor, Web-Filtering, Web-Mgmt-Console, Web-Scripting-Tools | Out-Null
            } else {
                Enable-WindowsOptionalFeature -Online -FeatureName IIS-WebServerRole, IIS-WebServer, IIS-StaticContent, IIS-DefaultDocument, IIS-HttpErrors, IIS-HttpLogging, IIS-RequestFiltering, IIS-ManagementConsole, IIS-ManagementScriptingTools -All -NoRestart | Out-Null
            }
        }
    } else {
        throw "IIS / WebAdministration yok. Once IIS'i kurun ya da -IISKur ile calistirin."
    }
}
if (-not $KuruCalisma) { Import-Module WebAdministration }

$hpp = $null
if (-not $KuruCalisma) { $hpp = Get-WebGlobalModule -Name "httpPlatformHandler" -ErrorAction SilentlyContinue }
if (-not $hpp -and -not $KuruCalisma) {
    if ($HttpPlatformMsi -and (Test-Path $HttpPlatformMsi)) {
        Yap "HttpPlatformHandler kuruluyor: $HttpPlatformMsi" { Start-Process msiexec.exe -ArgumentList "/i `"$HttpPlatformMsi`" /qn /norestart" -Wait; iisreset | Out-Null }
    } else {
        Write-Warning "HttpPlatformHandler 1.2 kurulu degil. MSI'yi indirip -HttpPlatformMsi <yol> ile verin: https://www.iis.net/downloads/microsoft/httpplatformhandler"
    }
}

# ------------------------------------------------------------ 1) Port / site cakisma kontrolu
Adim "1) Cakisma kontrolu"
if (-not $KuruCalisma) {
    $kullanan = Get-WebBinding -Protocol http -ErrorAction SilentlyContinue | Where-Object { $_.bindingInformation -match (":" + $Port + ":") }
    foreach ($b in $kullanan) {
        $sahip = ($b.ItemXPath -replace ".*@name='([^']+)'.*", '$1')
        if ($sahip -ne $Site) { throw "Port $Port zaten '$sahip' sitesinde kullaniliyor. Baska -Port secin." }
    }
    $mevcutYol = Get-Website | Where-Object { $_.physicalPath -eq $Kok -and $_.name -ne $Site }
    if ($mevcutYol) { throw "Bu klasor baska sitede kullaniliyor: $($mevcutYol.name)" }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne 4 }) {
        Write-Warning "Port $Port baska bir surec tarafindan dinleniyor olabilir (IIS disi). Kontrol edin."
    }
} else { Write-Host "[KURU] Port $Port ve site adi cakismasi kontrol edilecek" -ForegroundColor DarkYellow }

# ------------------------------------------------------------ 2) Python + venv
Adim "2) Python venv"
function Test-RealPython([string]$p) {
    if (-not $p -or -not (Test-Path -LiteralPath $p)) { return $false }
    if ($p -match '\\WindowsApps\\') { return $false }
    return ((Get-Item -LiteralPath $p).Length -gt 1024)
}
function Get-PyInfo([string]$exe) {
    try {
        $o = & $exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(64 if sys.maxsize > 2**32 else 32)" 2>$null
        $l = @($o | Where-Object { $_ })
        if ($l.Count -lt 2) { return $null }
        return @{ Version = [version]$l[0]; Bits = [int]$l[1]; Exe = $exe }
    } catch { return $null }
}
function Find-Python {
    $adaylar = New-Object System.Collections.Generic.List[string]
    if ($env:DYS_PYTHON) { $adaylar.Add($env:DYS_PYTHON) }
    foreach ($c in @("python", "python3", "py")) { $g = Get-Command $c -ErrorAction SilentlyContinue; if ($g -and $g.Source) { $adaylar.Add($g.Source) } }
    foreach ($d in @("C:\Program Files\Python310", "C:\Program Files\Python311", "C:\Program Files\Python312", "C:\Python310", "C:\Python311", "C:\Python312")) { $adaylar.Add((Join-Path $d "python.exe")) }
    $en = $null
    foreach ($a in $adaylar) {
        if (-not (Test-RealPython $a)) { continue }
        $exe = $a
        if ([IO.Path]::GetFileName($a) -ieq "py.exe") {
            $exe = (& $a -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if (-not (Test-RealPython $exe)) { continue }
        }
        $i = Get-PyInfo $exe
        if ($i -and $i.Version.Major -eq 3 -and $i.Version.Minor -ge 10 -and $i.Bits -eq 64) { if (-not $en -or $i.Version -gt $en.Version) { $en = $i } }
    }
    return $en
}
$venvPy = Join-Path $Kok ".venv\Scripts\python.exe"
$py = Find-Python
if (-not $py -and -not (Test-Path $venvPy)) {
    $kurPy = $false
    if ($KuruCalisma) { Write-Host "[KURU] Python 3.10 (64-bit) bulunamadi; kurulum gerekebilir" -ForegroundColor DarkYellow }
    else {
        Write-Warning "Python 3.10+ (64-bit) bulunamadi."
        $w = Get-Command winget -ErrorAction SilentlyContinue
        if ($w) { & winget install -e --id Python.Python.3.10 --scope machine --accept-package-agreements --accept-source-agreements; Refresh-ProcessPath; $py = Find-Python }
        if (-not $py) { throw "Python 3.10+ (64-bit) kurup `$env:DYS_PYTHON='C:\Program Files\Python310\python.exe' ile yeniden calistirin." }
    }
}
if ($py) { Write-Host ("Python: {0} ({1}-bit {2})" -f $py.Exe, $py.Bits, $py.Version) }
Yap "venv + requirements" {
    if (-not (Test-Path $venvPy)) { & $py.Exe -m venv (Join-Path $Kok ".venv"); if ($LASTEXITCODE -ne 0) { throw "venv olusturulamadi" } }
    & $venvPy -m pip install -U pip
    & $venvPy -m pip install -r (Join-Path $Kok "requirements.txt")
    New-Item -ItemType Directory -Force -Path (Join-Path $Kok "logs"), (Join-Path $Kok "temp") | Out-Null
}

# ------------------------------------------------------------ 3) web.config
Adim "3) web.config"
$wc = Join-Path $Kok "web.config"
if (-not $KuruCalisma -and -not (Test-Path $wc)) { throw "web.config yok: $wc" }
Yap "web.config guncelleniyor (processPath, log, PYTHONPATH, VIRTUAL_ENV; PYTHONHOME / UPLOAD / TEMP degiskenleri CIKARILIR - .env belirler)" {
    $xml = New-Object System.Xml.XmlDocument
    $xml.PreserveWhitespace = $true
    $xml.Load($wc)
    $hp = $xml.SelectSingleNode("//httpPlatform")
    if (-not $hp) { throw "httpPlatform dugumu yok" }
    $hp.SetAttribute("processPath", [string]$venvPy)
    $hp.SetAttribute("stdoutLogFile", [string](Join-Path $Kok "logs\httpplatform"))
    $sil = @()
    foreach ($ev in $hp.SelectNodes("environmentVariables/environmentVariable")) {
        switch ($ev.GetAttribute("name")) {
            "PYTHONPATH"          { $ev.SetAttribute("value", [string]$Kok) }
            "VIRTUAL_ENV"         { $ev.SetAttribute("value", [string](Join-Path $Kok ".venv")) }
            "PYTHONHOME"          { $sil += $ev }
            "DYS_UPLOAD_FOLDER"   { $sil += $ev }
            "DYS_TEMP_FOLDER"     { $sil += $ev }
            "DYS_FILESHARE_USER"  { $sil += $ev }
            "DYS_FILESHARE_PASSWORD" { $sil += $ev }
        }
    }
    foreach ($n in $sil) { [void]$n.ParentNode.RemoveChild($n) }
    $xml.Save($wc)
}

# ------------------------------------------------------------ 4) IIS havuz + site
Adim "4) IIS havuzu ve sitesi"
Yap "havuz $Havuz (No Managed Code, 64-bit, bosta kapanma yok)" {
    if (-not (Test-Path "IIS:\AppPools\$Havuz")) { New-WebAppPool -Name $Havuz | Out-Null }
    Set-ItemProperty "IIS:\AppPools\$Havuz" -Name managedRuntimeVersion -Value ""
    Set-ItemProperty "IIS:\AppPools\$Havuz" -Name enable32BitAppOnWin64 -Value $false
    Set-ItemProperty "IIS:\AppPools\$Havuz" -Name processModel.idleTimeout -Value ([TimeSpan]::FromMinutes(0))
    Set-ItemProperty "IIS:\AppPools\$Havuz" -Name processModel.identityType -Value $(if ($HavuzLocalSystem) { "LocalSystem" } else { "ApplicationPoolIdentity" })
}
Yap "site $Site -> $Kok :$Port" {
    if (-not (Get-Website -Name $Site -ErrorAction SilentlyContinue)) { New-Website -Name $Site -Port $Port -PhysicalPath $Kok -ApplicationPool $Havuz | Out-Null }
    else {
        Set-ItemProperty "IIS:\Sites\$Site" -Name physicalPath -Value $Kok
        Set-ItemProperty "IIS:\Sites\$Site" -Name applicationPool -Value $Havuz
    }
}
$kural = "DEBAK-DYS $Port"
Yap "guvenlik duvari: TCP $Port gelen kural ($kural)" {
    if (-not (Get-NetFirewallRule -DisplayName $kural -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $kural -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    }
}
Yap "klasor izinleri (havuz kimligine okuma; logs / temp / uploads degistirme)" {
    $kimlik = if ($HavuzLocalSystem) { "NT AUTHORITY\SYSTEM" } else { "IIS AppPool\$Havuz" }
    icacls $Kok /grant "${kimlik}:(OI)(CI)RX" | Out-Null   # /T yok: miras alt klasorlere kendiliginden gecer (uzun yollu klasorlerde /T hata verir)
    foreach ($k in @("logs", "temp", "uploads")) {
        $d = Join-Path $Kok $k
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        icacls $d /grant "${kimlik}:(OI)(CI)M" | Out-Null
    }
}
Yap "site baslatiliyor" { Start-WebAppPool $Havuz -ErrorAction SilentlyContinue; Start-Website -Name $Site -ErrorAction SilentlyContinue }

# ------------------------------------------------------------ 5) Sonuc
Adim "5) Sonuc"
if (-not (Test-Path (Join-Path $Kok ".env"))) {
    Write-Warning ".env YOK. .env.example -> .env kopyalayip DOLDURUN (DYS_SECRET_KEY, veritabani, DYS_UPLOAD_FOLDER, DYS_FILESHARE_*). Sonra: iisreset veya havuzu yeniden baslatin."
}
Write-Host ("Smoke: http://localhost:{0}/login  ve  http://192.168.0.245:{0}/login" -f $Port)
Write-Host "Sonraki: GECIS_GUNU.md adim 7 (veri aktarimlari) ve scripts\duman_testi.py"
if ($KuruCalisma) { Write-Host "KURU CALISMA - hicbir sey degismedi." -ForegroundColor Yellow } else { Write-Host "Bitti." -ForegroundColor Green }
