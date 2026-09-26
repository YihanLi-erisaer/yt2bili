import { test, expect } from "@playwright/test";

test("AcFun opt-in requires an enabled account and preview mode", async ({ page }) => {
  await page.goto("/?preview&accounts=1");
  await page.getByRole("button", { name: /新建任务/ }).first().click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("checkbox", { name: /同步上传 AcFun/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();

  await page.goto("/?preview&accounts=1&acfun=1");
  await expect(page.getByText("AcFun 独立上传")).toBeVisible();
  await page.getByRole("button", { name: /新建任务/ }).first().click();
  dialog = page.getByRole("dialog");
  const sync = dialog.getByRole("checkbox", { name: /同步上传 AcFun/ });
  await expect(sync).toBeEnabled();
  await expect(sync).not.toBeChecked();
  await sync.check();
  await expect(dialog.getByText(/AcFun 使用独立队列/)).toBeVisible();
  await dialog.getByRole("radio", { name: /自动投稿/ }).check();
  await expect(sync).toBeDisabled();
});
