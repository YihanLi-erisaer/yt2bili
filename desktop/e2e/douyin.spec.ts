import { test, expect } from "@playwright/test";

test("Douyin synchronization is disabled without login", async ({ page }) => {
  await page.goto("/?preview&accounts=1");
  await page
    .getByRole("button", { name: /新建任务/ })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("checkbox", { name: /同步上传抖音/ }),
  ).toBeDisabled();
  await expect(dialog.getByText(/请先在“账号与连接”登录抖音/)).toBeVisible();
});

test("one logged-in Douyin account enables opt-in in both modes", async ({
  page,
}) => {
  await page.goto("/?preview&accounts=1&douyin=1");
  await page
    .getByRole("button", { name: /新建任务/ })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  const sync = dialog.getByRole("checkbox", { name: /同步上传抖音/ });
  await expect(sync).toBeEnabled();
  await expect(sync).not.toBeChecked();
  await sync.check();
  await expect(dialog.getByText(/预览模式一起确认/)).toBeVisible();
  await dialog.getByRole("radio", { name: /自动投稿/ }).check();
  await expect(sync).toBeChecked();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page
    .getByRole("button", { name: /新建任务/ })
    .first()
    .click();
  await expect(
    page.getByRole("dialog").getByRole("checkbox", { name: /同步上传抖音/ }),
  ).not.toBeChecked();
});
