import { test, expect } from "@playwright/test";

test("local-first translation settings and fallback can be changed", async ({
  page,
}) => {
  await page.goto("/?preview");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("首选翻译服务")).toHaveValue("local_llm");
  await page.getByLabel("首选翻译服务").selectOption("deepl");
  await expect(page.getByLabel("首选翻译服务")).toHaveValue("deepl");
  await page.getByLabel("首选失败时使用另一服务").uncheck();
  await expect(page.getByLabel("首选失败时使用另一服务")).not.toBeChecked();
  await expect(page.getByText(/不自动切换/)).toBeVisible();
  await expect(page.getByLabel("大模型推理超时（秒）")).toHaveValue("300");
  await expect(page.getByLabel("翻译流程总超时（秒）")).toHaveValue("420");
  await page.getByLabel("大模型推理超时（秒）").fill("240");
  await page.getByLabel("翻译流程总超时（秒）").fill("360");
  await page.getByRole("button", { name: "保存超时设置" }).click();
  await expect(page.getByLabel("大模型推理超时（秒）")).toHaveValue("240");
  await expect(page.getByLabel("翻译流程总超时（秒）")).toHaveValue("360");
});

test("first-run accepts local test without a DeepL key", async ({ page }) => {
  await page.goto("/?preview");
  await page.getByRole("button", { name: "开始配置" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "下一步" }).click();
  await dialog.getByRole("button", { name: "下一步" }).click();
  await expect(dialog.getByRole("button", { name: "下一步" })).toBeDisabled();
  await dialog.getByRole("button", { name: "本地试译", exact: true }).click();
  await expect(
    dialog.getByText("更好的工作流", { exact: false }),
  ).toBeVisible();
  await expect(dialog.getByRole("button", { name: "下一步" })).toBeEnabled();
  await expect(dialog.getByLabel("DeepL API 密钥")).toHaveValue("");
});

test("retranslation requires explicit replacement confirmation", async ({
  page,
}) => {
  await page.goto("/?preview&populated");
  await page.getByRole("button", { name: /用更少的工具/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "按当前设置重新翻译" }).click();
  await expect(dialog).toContainText("含人工修改");
  await dialog.getByRole("button", { name: "确认", exact: true }).click();
  await expect(dialog.getByLabel(/中文标题/)).toHaveValue("重新翻译的标题");
});

test("first-run guide starts with storage selection", async ({ page }) => {
  await page.goto("/?preview");
  await page.getByRole("button", { name: "开始配置" }).click();
  await expect(page.getByRole("dialog")).toContainText("欢迎使用 yt2bili");
  await expect(page.getByLabel("素材工作目录")).toHaveValue(/work/);
  await expect(page.getByRole("dialog")).toContainText("运行环境");
  await page.getByRole("button", { name: "关闭对话框" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("empty workspace, preview default, and modal keyboard focus", async ({
  page,
}) => {
  await page.goto("/?preview&accounts=5");
  await expect(page.getByText("你的下一条视频，从这里开始")).toBeVisible();
  await page.getByRole("button", { name: /新建任务/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radio").first()).toBeChecked();
  await expect(dialog.getByRole("button", { name: "加入队列" })).toBeDisabled();
  await dialog.getByRole("textbox").fill("https://youtu.be/abcdefghijk");
  await dialog.getByRole("checkbox").check();
  await expect(dialog.getByRole("button", { name: "加入队列" })).toBeDisabled();
  await dialog.getByLabel("目标 Bilibili 账号").selectOption("account-5");
  await dialog.getByRole("button", { name: "加入队列" }).click();
  await expect(dialog.getByRole("alert")).toContainText("界面预览");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
});

test("task preview preserves unsaved edits and gates submission", async ({
  page,
}) => {
  await page.goto("/?preview&populated&accounts=1");
  await page.getByRole("button", { name: /用更少的工具/ }).click();
  const dialog = page.getByRole("dialog");
  const title = dialog.getByLabel(/中文标题/);
  await title.fill("手动编辑的标题");
  await page.waitForTimeout(1700);
  await expect(title).toHaveValue("手动编辑的标题");
  await expect(
    dialog.getByRole("button", { name: "确认投稿", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(
    dialog.getByRole("button", { name: "确认投稿", exact: true }),
  ).toBeEnabled();
});

test("settings theme and 1024px layout", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 680 });
  await page.goto("/?preview");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByRole("button", { name: "浅色", exact: true }).click();
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > innerWidth,
  );
  expect(overflow).toBe(false);
});

test("production-style unconnected page never pretends to perform work", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("未连接到后台")).toBeVisible();
  await expect(page.getByRole("button", { name: /新建任务/ })).toBeDisabled();
});
test("five accounts have seven lanes and account capacity is enforced", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 680 });
  await page.goto("/?preview&accounts=5");
  await expect(page.locator(".queue-card")).toHaveCount(7);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "账号与连接", exact: true }).click();
  await expect(page.getByText("哔哩哔哩账号 · 5/5")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "添加账号", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "重新登录", exact: true }),
  ).toHaveCount(5);
});

test("no account cannot create and historical task requires binding", async ({
  page,
}) => {
  await page.goto("/?preview&populated");
  await page.getByRole("button", { name: /用更少的工具/ }).click();
  await expect(page.getByRole("dialog")).toContainText("历史账号待确认");
  await expect(
    page.getByRole("button", { name: "确认投稿", exact: true }),
  ).toBeDisabled();
});

test("five copies of one video keep separate edits and fifth-account filter", async ({
  page,
}) => {
  await page.goto("/?preview&populated&accounts=5&samevideo");
  await expect(page.locator(".task-row")).toHaveCount(5);
  await page.getByLabel("按账号筛选").selectOption("account-5");
  await expect(page.locator(".task-row")).toHaveCount(1);
  await page.locator(".task-row").click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("UID 10005");
  await dialog.getByLabel(/中文标题/).fill("仅第五账号的标题");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await page.getByRole("button", { name: "关闭对话框" }).click();
  await page.getByLabel("按账号筛选").selectOption("account-1");
  await expect(page.locator(".task-row")).toContainText("用更少的工具");
});
