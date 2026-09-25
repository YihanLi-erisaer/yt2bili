#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::{collections::HashMap, io::{BufRead, BufReader, Write}, path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio}, sync::{Arc, Mutex, atomic::{AtomicU64, Ordering}}, time::Duration};
use tauri::{Emitter, Manager, State};
use tokio::sync::oneshot;

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, String>>>>>;
struct Worker {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    pending: Pending,
    next: AtomicU64,
    #[cfg(windows)]
    job: Mutex<Option<usize>>,
}

struct WorkerProcess {
    child: Option<Child>,
    #[cfg(windows)]
    job: Option<usize>,
}

async fn request_worker(worker: &Worker, method: String, params: Value) -> Result<Value, String> {
    if method.len() > 100 || !params.is_object() { return Err("无效请求".into()); }
    let id = worker.next.fetch_add(1, Ordering::Relaxed).to_string();
    let (tx, rx) = oneshot::channel();
    worker.pending.lock().unwrap().insert(id.clone(), tx);
    let message = json!({"protocol_version":2,"request_id":id,"method":method,"params":params}).to_string();
    if message.len() > 1_000_000 {
        worker.pending.lock().unwrap().remove(&id);
        return Err("请求超过大小限制".into());
    }
    let result = {
        let mut input = worker.stdin.lock().unwrap();
        match input.as_mut() {
            Some(pipe) => writeln!(pipe, "{}", message).and_then(|_| pipe.flush()).map_err(|e| e.to_string()),
            None => Err("后台已停止，请重新启动应用。".into()),
        }
    };
    if let Err(error) = result { worker.pending.lock().unwrap().remove(&id); return Err(error); }
    match tokio::time::timeout(Duration::from_secs(90), rx).await {
        Ok(Ok(value)) => value,
        _ => { worker.pending.lock().unwrap().remove(&id); Err("后台响应超时；请刷新状态后再操作，勿重复投稿。".into()) }
    }
}

#[tauri::command]
async fn backend_request(worker: State<'_, Worker>, method: String, params: Value) -> Result<Value, String> {
    request_worker(&worker, method, params).await
}

fn take_worker_process(worker: &Worker) -> WorkerProcess {
    worker.stdin.lock().unwrap().take();
    WorkerProcess {
        child: worker.child.lock().unwrap().take(),
        #[cfg(windows)]
        job: worker.job.lock().unwrap().take(),
    }
}

fn reap_worker(mut process: WorkerProcess) {
    if let Some(mut child) = process.child.take() {
        for _ in 0..30 {
            if child.try_wait().ok().flatten().is_some() { return close_worker_job(process); }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    close_worker_job(process);
}

fn force_stop_worker(mut process: WorkerProcess) {
    // Exit callbacks run on the UI event thread. Never wait for Python here:
    // closing the Windows job handle terminates its whole process tree.
    #[cfg(windows)]
    if let Some(job) = process.job.take() {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(job as _); }
    }
    if let Some(mut child) = process.child.take() { let _ = child.kill(); }
}

#[cfg(windows)]
fn close_worker_job(mut process: WorkerProcess) {
    if let Some(job) = process.job.take() {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(job as _); }
    }
}

#[cfg(not(windows))]
fn close_worker_job(_: WorkerProcess) {}

#[tauri::command]
async fn finish_close(app: tauri::AppHandle, worker: State<'_, Worker>) -> Result<(), String> {
    let status = request_worker(&worker, "system.shutdown_status".into(), json!({})).await?;
    if status["ready"] != true { return Err("后台仍在收尾，请等待上传完成。".into()); }
    let process = take_worker_process(&worker);
    tauri::async_runtime::spawn_blocking(move || reap_worker(process)).await
        .map_err(|_| "后台退出任务未完成，请重试。".to_string())?;
    app.exit(0);
    Ok(())
}

#[tauri::command]
fn frontend_ready(app: tauri::AppHandle, health: Value) {
    if smoke_enabled() {
        if let Some(report) = std::env::var_os("YT2BILI_NATIVE_SMOKE_REPORT") {
            let value = json!({"ok":health["protocol_version"] == 2,"webview_loaded":true,
                "frontend_ipc":true,"protocol_version":health["protocol_version"]});
            let _ = std::fs::write(report, value.to_string());
            app.exit(0);
        }
    }
}

fn smoke_enabled() -> bool {
    std::env::var_os("YT2BILI_NATIVE_SMOKE_REPORT").is_some()
        && std::env::var_os("YT2BILI_DESKTOP_DATA").is_some()
        && (cfg!(debug_assertions) || std::env::args().any(|arg| arg == "--smoke-test"))
}

#[cfg(windows)]
fn attach_job(child: &Child) -> Result<usize, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::*;
    use windows_sys::Win32::Foundation::CloseHandle;
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() { return Err("无法创建后台进程组".into()); }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info as *const _ as *const _, std::mem::size_of_val(&info) as u32) == 0
            || AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0 {
            CloseHandle(job);
            return Err("无法保护后台进程生命周期".into());
        }
        Ok(job as usize)
    }
}

fn start_worker(app: &tauri::AppHandle) -> Result<Worker, Box<dyn std::error::Error>> {
    let project = std::env::var_os("YT2BILI_PROJECT_ROOT").map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf());
    let resources = if cfg!(debug_assertions) {
        std::env::var_os("YT2BILI_RESOURCES").map(PathBuf::from).unwrap_or_else(|| project.clone())
    } else {
        app.path().resource_dir()?
    };
    let data = std::env::var_os("YT2BILI_DESKTOP_DATA").map(PathBuf::from)
        .unwrap_or(app.path().local_data_dir()?.join("StarDazz").join("yt2bili"));
    let mut command = if !cfg!(debug_assertions) {
        Command::new(resources.join("worker").join(if cfg!(windows) { "yt2bili-worker.exe" } else { "yt2bili-worker" }))
    } else if let Some(frozen) = std::env::var_os("YT2BILI_WORKER") {
        Command::new(frozen)
    } else {
        let python = std::env::var_os("YT2BILI_PYTHON").map(PathBuf::from).unwrap_or_else(|| {
            project.join(if cfg!(windows) { ".desktop-venv/Scripts/python.exe" } else { ".desktop-venv/bin/python" })
        });
        let mut cmd = Command::new(python);
        cmd.args(["-u", "-m", "yt2bili.desktop_worker"]);
        cmd
    };
    command.arg("--data-dir").arg(data).arg("--resources").arg(&resources)
        .current_dir(if cfg!(debug_assertions) { &project } else { &resources }).env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)] {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    #[cfg(unix)] {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn()?;
    #[cfg(windows)]
    let job = match attach_job(&child) { Ok(job) => job, Err(error) => { let _ = child.kill(); return Err(error.into()); } };
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().ok_or("worker stdout missing")?;
    let stderr = child.stderr.take().ok_or("worker stderr missing")?;
    // Drain diagnostic output; protocol logs are already sanitized by Python.
    std::thread::spawn(move || { for line in BufReader::new(stderr).lines() { if line.is_err() { break; } } });
    let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
    let replies = pending.clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            let Ok(value) = serde_json::from_str::<Value>(&line) else { continue };
            if let Some(id) = value.get("request_id").and_then(Value::as_str) {
                if let Some(sender) = replies.lock().unwrap().remove(id) {
                    let answer = if let Some(error) = value.get("error") {
                        Err(error.get("message").and_then(Value::as_str).unwrap_or("后台操作失败").to_owned())
                    } else { Ok(value["result"].clone()) };
                    let _ = sender.send(answer);
                }
            } else if value.get("event").is_some() {
                let _ = handle.emit("backend-event", value);
            }
        }
        for (_, sender) in replies.lock().unwrap().drain() { let _ = sender.send(Err("后台进程已停止，请重新启动应用。".into())); }
        let _ = handle.emit("backend-disconnected", ());
    });
    Ok(Worker { child: Mutex::new(Some(child)), stdin: Mutex::new(stdin), pending, next: AtomicU64::new(1),
        #[cfg(windows)] job: Mutex::new(Some(job)) })
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            app.manage(start_worker(app.handle())?);
            if smoke_enabled() {
                if let Some(window) = app.get_webview_window("main") { let _ = window.hide(); }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_request, finish_close, frontend_ready])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.emit("app-close-requested", ());
            }
        })
        .build(tauri::generate_context!())
        .expect("无法启动桌面应用；请检查 Python 环境与后台日志。");
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            let worker = app.state::<Worker>();
            force_stop_worker(take_worker_process(&worker));
        }
    });
}
