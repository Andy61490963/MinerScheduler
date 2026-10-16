# MinerScheduler

Windows 挖礦排程與即時效率監控，將正式核心和測試分成兩個專案。

監控畫面使用 **http://127.0.0.1:18776/**，顯示算力、耗電、每瓦效率、核心／熱點／顯存溫度及五組風扇轉速；可直接開始、停止挖礦，查看 LOG，保存日間／離峰功耗與風扇設定。

## 專案結構

```text
CORE/
├─ src/mining/          啟動流程與礦工程序管理
├─ src/safety/          功耗、啟動驗證及即時保護心跳
├─ src/hardware/        時段規則、GPU 讀值及風扇控制
├─ src/coordination/    本地模型忙碌暫停、閒置恢復
├─ src/dashboard/       監控資料、啟停與設定 API
├─ web/                 監控網頁、圖表與 LOG
├─ windows/             Windows 登入、排程及啟動腳本
├─ config/              功耗與 FanControl 範例設定
├─ docs/                操作說明
├─ project.py           檢查與建置入口
└─ project.json         部署檔名、來源雜湊與依賴清單
TESTS/
├─ tests/               獨立離線回歸測試
├─ tools/               需另行授權執行的實機功耗短測
└─ run_tests.py         隔離測試入口
```

## 功耗與保護

正常啟動及挖礦直接依照網頁保存的時段功耗，移除先以150W挖礦一分鐘的限制。範例快照為日間／離峰350W、GPU兩組風扇80%、主機板三組50%；實際運行以本機保存值為準。

核心80°C、熱點105°C或顯存90°C會降瓦，核心82°C或感測失效會停止。保護初始化、風扇切換、停止及異常時仍使用低功耗安全基準。功耗限制在原廠允許範圍內；人工停止優先，礦工以一般使用者權限運行，SYSTEM 排程只執行功耗保護。

本地模型忙碌時暫停，閒置60秒後恢復。登入入口 `CORE/windows/login.ps1` 先啟動監控網站，再處理風扇與原有挖礦啟動核對；人工停挖狀態會保留。

## 使用環境與個人設定

本版本依既有 Windows、RTX 4090 及主機板 FanControl 接頭配置整理，部署路徑沿用 `C:\test\CORE`。Python 使用3.12；礦工為 SRBMiner-MULTI 3.7.6，FanControl 範例設定版本為283。第三方執行檔、Python 執行環境、私人設定及實機紀錄由本機部署另行保管，必要依賴雜湊列於 `CORE/project.json`。

公開版本已將收款地址換成 `__TRC20_WALLET__`、顯卡識別碼換成 `GPU-__LOCAL_UUID__`。建立本機版本時，須一致替換設定與程式中的佔位值，並依實際硬體核對 FanControl 接頭與依賴；固定識別核對保留啟用。可用以下命令找出需要個人化的位置：

```powershell
rg -n '__TRC20_WALLET__|GPU-__LOCAL_UUID__' CORE TESTS
```

儲存庫中的 `config` 是範例快照。更新已部署程式時須整合當時保存的本機設定，避免覆蓋後續在監控畫面調整的值。

## 檢查、測試與建置

以下命令在 Windows 專案根目錄執行，並需要已有 `C:\test\CORE` 的已核對第三方依賴。使用一般 Python3.12，或將 `python` 換成該部署的 `TEST_環境\python.exe`。

```powershell
python -B CORE/project.py check
python -B TESTS/run_tests.py
python -B CORE/project.py build --output C:\test\建置\新版本
```

建置只組裝相容檔名至全新目錄，部署需另行安排。離線測試使用臨時版本與模擬硬體，阻擋正式目錄寫入、外部程序、顯卡控制介面及正式監控／模型連線；目前80項測試涵蓋設定保存、啟停競爭、依保存功耗啟動、時段邊界、溫控與感測保護。

`TESTS/tools/功耗短測.py` 會操作真實負載，必須在明確授權及硬體確認後另行執行。每檔5秒的短測僅能觀察瞬時反應，長時間穩定性需另外驗證。
