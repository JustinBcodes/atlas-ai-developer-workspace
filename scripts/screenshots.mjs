import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const executablePath = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
await mkdir("screenshots", { recursive: true });
await page.goto(process.env.APP_URL || "http://localhost:3000", { waitUntil: "networkidle" });
await page.screenshot({ path: "screenshots/welcome.png", fullPage: true });
await page.getByRole("button", { name: /Explore demo/ }).click();
await page.screenshot({ path: "screenshots/dashboard.png", fullPage: true });
await page.getByRole("button", { name: /Code explorer/ }).click();
await page.getByRole("button", { name: /src\/services\/indexer.ts/ }).click();
await page.screenshot({ path: "screenshots/explorer.png", fullPage: true });
await browser.close();
