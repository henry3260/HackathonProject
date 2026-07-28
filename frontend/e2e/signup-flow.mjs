/**
 * 註冊帳號的瀏覽器端到端測試。
 *
 * 真的在 UI 上註冊一組新帳號，然後把登入後的功能全跑一遍：
 * 服務卡片填單 → 案件明細狀態流轉 → 我的服務 → AI 管家對話建案 →
 * 登出後用同一組帳密登入，案件仍在。
 *
 * 第一次要先裝瀏覽器（npm install 只裝 Playwright 套件，不含瀏覽器本體）：
 *   cd frontend && npx playwright install chromium
 *
 * 先啟動後端與前端，再執行：
 *   cd backend && uvicorn app.main:app --reload
 *   cd frontend && npm run dev
 *   cd frontend && npm run test:e2e
 *
 * 環境變數：
 *   E2E_BASE_URL    前端網址（預設 http://127.0.0.1:5173）
 *   E2E_HEADED=1    顯示瀏覽器視窗
 *   E2E_SHOT_DIR    截圖輸出資料夾（預設不截圖）
 *   CHROMIUM_PATH   指定 Chromium 執行檔（Playwright 找不到瀏覽器時使用）
 */
import { chromium } from "playwright";

const BASE_URL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:5173";
const SHOT_DIR = process.env.E2E_SHOT_DIR ?? "";
const PASSWORD = "Passw0rd123";
const EMAIL = `e2e-${Date.now()}@example.com`;

const log = (...parts) => console.log(...parts);
const step = (title) => log(`\n=== ${title} ===`);

let stepIndex = 0;
const failures = [];

function check(label, condition, detail = "") {
  if (condition) {
    log(`  ✓ ${label}`);
    return true;
  }
  const message = detail ? `${label} — ${detail}` : label;
  log(`  ✗ ${message}`);
  failures.push(message);
  return false;
}

async function statusOf(url) {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(3000) })).status;
  } catch {
    return 0; // 連不上
  }
}

/** 先確認兩個 server 都在跑，否則測試會爛在 goto 上、看不出原因。 */
async function preflight() {
  if ((await statusOf(BASE_URL)) === 0) {
    log(`前端 dev server 沒有回應：${BASE_URL}`);
    log("請在另一個終端機保持這個指令執行中：");
    log("  cd frontend && npm run dev");
    return false;
  }
  // /health 由 vite 代理到後端；後端沒開時 vite 會回 500，所以不能只看「有回應」。
  const health = await statusOf(`${BASE_URL}/health`);
  if (health !== 200) {
    log(`後端 API 沒有回應（前端有在跑，但 /health 回 ${health || "連不上"}）。`);
    log("請在另一個終端機保持這個指令執行中：");
    log("  cd backend && source .venv/bin/activate && uvicorn app.main:app --reload");
    return false;
  }
  return true;
}

async function main() {
  if (!(await preflight())) process.exit(1);

  const browser = await chromium.launch({
    headless: !process.env.E2E_HEADED,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  const page = await browser.newPage({ viewport: { width: 430, height: 932 } });

  const pageErrors = [];
  const apiErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", async (response) => {
    if (!response.url().includes("/api/") || response.status() < 400) return;
    let body = "";
    try {
      body = (await response.text()).slice(0, 200);
    } catch {
      /* 忽略讀不到的 body */
    }
    apiErrors.push(
      `${response.status()} ${response.request().method()} ${new URL(response.url()).pathname} :: ${body}`,
    );
  });

  const shot = async (name) => {
    if (!SHOT_DIR) return;
    stepIndex += 1;
    await page.screenshot({
      path: `${SHOT_DIR}/${String(stepIndex).padStart(2, "0")}-${name}.png`,
      fullPage: true,
    });
  };
  const mainText = async () => (await page.locator("main").innerText()).replace(/\n+/g, " | ");
  // 案件明細會先顯示「正在載入案件資料…」，讀內容前要等它換成真的資料。
  const waitForRequestDetail = async () => {
    await page.waitForFunction(
      () => !(document.querySelector("main")?.innerText ?? "").includes("正在載入案件資料"),
      undefined,
      { timeout: 15_000 },
    );
    return mainText();
  };

  try {
    step(`1. 在 UI 上註冊新帳號（${EMAIL}）`);
    await page.goto(`${BASE_URL}/login`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "註冊", exact: true }).click();
    await page.getByPlaceholder("姓名（選填）").fill("E2E 測試用戶");
    await page.getByPlaceholder("Email").fill(EMAIL);
    await page.getByPlaceholder("密碼（至少 8 碼）").fill(PASSWORD);
    await page.getByRole("button", { name: "註冊", exact: true }).click();
    await page.waitForURL("**/home", { timeout: 15_000 });
    check("註冊後進入首頁", page.url().includes("/home"));
    check(
      "首頁顯示註冊時填的名字",
      (await page.locator("p.text-brand").first().innerText()).includes("E2E 測試用戶"),
    );
    await shot("home");

    step("2. 用服務卡片填單建立案件");
    await page.getByRole("button", { name: /冷氣清洗/ }).click();
    await page.waitForURL("**/services/air_conditioner_cleaning");
    await page.locator("input[type=number]").first().fill("2");
    await page.locator("input[type=date]").first().fill(futureDateIso());
    await page.locator("select").first().selectOption({ index: 1 });
    const textInputs = page.locator("input[type=text], input:not([type])");
    const textCount = await textInputs.count();
    await textInputs.nth(textCount - 2).fill("台北市大安區忠孝東路四段 100 號 5 樓");
    await textInputs.nth(textCount - 1).fill("0912345678");
    await shot("form-filled");
    await page.getByRole("button", { name: /送出|建立|申請/ }).last().click();
    await page.waitForURL("**/requests/**", { timeout: 15_000 });
    const formRequestDetail = await waitForRequestDetail();
    check("表單送出後導到案件明細", page.url().includes("/requests/"));
    check("明細保留完整地址（含門牌）", formRequestDetail.includes("100 號 5 樓"), formRequestDetail);
    await shot("request-detail");

    step("3. 案件狀態流轉");
    const seenStatuses = [];
    for (let round = 0; round < 4; round += 1) {
      const button = page.getByRole("button", { name: /Demo：/ }).first();
      if (!(await button.count())) break;
      await button.click();
      await page.waitForTimeout(1200);
      seenStatuses.push((await mainText()).split(" | ")[2]);
    }
    check("案件可以一路推到已完成", seenStatuses.includes("已完成"), seenStatuses.join(" → "));

    step("4. 我的服務列出剛建立的案件");
    await page.goto(`${BASE_URL}/my-services`, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    const listText = await mainText();
    check("清單出現冷氣清洗案件", listText.includes("冷氣清洗"), listText);
    await shot("my-services");

    step("5. AI 管家對話建立案件");
    await page.goto(`${BASE_URL}/home`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /啟動 AI 管家/ }).click();
    const chatInput = page.getByLabel("輸入需求");
    await chatInput.waitFor({ timeout: 10_000 });

    const turns = [
      "我要預約洗衣機清洗",
      "兩台",
      "滾筒式",
      futureDateIso(),
      "下午",
      "台北市大安區忠孝東路四段 100 號 5 樓",
      "0912345678",
    ];
    for (const message of turns) {
      // 欄位收齊後聊天輸入框會換成確認畫面。
      if (await page.getByRole("button", { name: "確認送出" }).count()) break;
      await chatInput.fill(message);
      await chatInput.press("Enter");
      await page.waitForTimeout(2500);
    }
    await shot("butler-chat");

    const confirmButton = page.getByRole("button", { name: "確認送出" });
    if (check("對話收齊欄位後出現確認畫面", (await confirmButton.count()) > 0, await mainText())) {
      await confirmButton.click();
      await page.waitForURL("**/requests/**", { timeout: 15_000 });
      const chatRequestDetail = await waitForRequestDetail();
      check("AI 管家真的建立了案件", page.url().includes("/requests/"));
      check("案件記錄了對話收到的欄位", chatRequestDetail.includes("滾筒式"), chatRequestDetail);
      await shot("butler-request-detail");
    }

    step("6. 登出後用同一組帳密登入，案件仍在");
    await page.goto(`${BASE_URL}/home`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "登出" }).click();
    await page.waitForURL("**/login");
    await page.getByRole("button", { name: "登入", exact: true }).click();
    await page.getByPlaceholder("Email").fill(EMAIL);
    await page.getByPlaceholder("密碼").fill(PASSWORD);
    await page.getByRole("button", { name: "登入", exact: true }).click();
    await page.waitForURL("**/home", { timeout: 15_000 });
    check("同一組帳密可以重新登入", page.url().includes("/home"));

    await page.goto(`${BASE_URL}/my-services`, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    const afterRelogin = await mainText();
    check("重新登入後仍看得到兩筆案件", afterRelogin.includes("冷氣清洗") && afterRelogin.includes("洗衣機清洗"), afterRelogin);
    await shot("after-relogin");
  } catch (error) {
    failures.push(`未預期的錯誤：${error.message}`);
    log(`\n!!! ${error.message}`);
    await shot("failure");
  }

  step("結果");
  check("沒有前端 JS 錯誤", pageErrors.length === 0, pageErrors.join("; "));
  check("沒有失敗的 API 呼叫", apiErrors.length === 0, apiErrors.join("; "));

  await browser.close();

  if (failures.length) {
    log(`\n${failures.length} 項失敗：`);
    failures.forEach((failure) => log(`  - ${failure}`));
    process.exit(1);
  }
  log("\n全部通過。");
}

function futureDateIso() {
  const target = new Date(Date.now() + 8 * 24 * 60 * 60 * 1000);
  return target.toISOString().slice(0, 10);
}

await main();
