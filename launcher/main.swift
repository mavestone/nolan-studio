// Nolan launcher — tiny Cocoa stub that:
//   • Registers as a proper app with WindowServer (so the dock indicator
//     dot shows + no infinite bouncing)
//   • Spawns the Python server as a child process
//   • Waits for localhost:8765, then opens the browser
//   • Stays alive in the NSApplication run loop until Cmd+Q / Dock → Quit
//   • Cleanly terminates the Python child on shutdown
//
// Compile:
//   swiftc -O -o nolan-launcher main.swift
//   (universal: -arch arm64 -arch x86_64)

import Cocoa
import Foundation

// ── Paths ─────────────────────────────────────────────────────────────────────
let execURL = URL(fileURLWithPath: CommandLine.arguments[0])
// Walk up to find the Nolan repo root: this binary sits at
//   <ROOT>/Nolan.app/Contents/MacOS/nolan-launcher
// AppData reads a sibling file `nolan-root.txt` next to the binary so we know
// where to cd into, regardless of where the .app got moved.
let bundleResources = execURL.deletingLastPathComponent()  // Contents/MacOS
    .deletingLastPathComponent()                            // Contents
    .appendingPathComponent("Resources")
let rootHintFile = bundleResources.appendingPathComponent("nolan-root.txt")

func readRoot() -> String? {
    guard let txt = try? String(contentsOf: rootHintFile, encoding: .utf8) else { return nil }
    let trimmed = txt.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

guard let nolanRoot = readRoot(), FileManager.default.fileExists(atPath: nolanRoot) else {
    let alert = NSAlert()
    alert.messageText = "Nolan source folder missing"
    alert.informativeText = "Couldn't read \(rootHintFile.path).\n\nRe-run install.sh from the Nolan repo."
    alert.alertStyle = .critical
    alert.runModal()
    exit(1)
}

// ── Log file ──────────────────────────────────────────────────────────────────
let logDir = ("~/Library/Logs/Nolan" as NSString).expandingTildeInPath
try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
let serverLog  = "\(logDir)/server.log"
let launcherLog = "\(logDir)/launcher.log"

// Rotate server log
if FileManager.default.fileExists(atPath: serverLog) {
    try? FileManager.default.removeItem(atPath: serverLog + ".prev")
    try? FileManager.default.moveItem(atPath: serverLog, toPath: serverLog + ".prev")
}

func log(_ msg: String) {
    let line = "\(Date()) \(msg)\n"
    if let data = line.data(using: .utf8) {
        if let h = FileHandle(forWritingAtPath: launcherLog) {
            h.seekToEndOfFile()
            h.write(data)
            try? h.close()
        } else {
            try? line.write(toFile: launcherLog, atomically: false, encoding: .utf8)
        }
    }
}

// Truncate launcher log on each boot
try? "".write(toFile: launcherLog, atomically: true, encoding: .utf8)
log("Nolan launcher starting")
log("Root: \(nolanRoot)")

// ── Detect hardware arch (handles Rosetta shells) ─────────────────────────────
func runShell(_ cmd: String, _ args: [String]) -> (Int32, String) {
    let p = Process()
    p.launchPath = cmd
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "") }
    p.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

let (_, arm64Probe) = runShell("/usr/sbin/sysctl", ["-n", "hw.optional.arm64"])
let isArm64 = arm64Probe.trimmingCharacters(in: .whitespacesAndNewlines) == "1"
let archOrder: [String] = isArm64 ? ["arm64", "x86_64"] : ["x86_64", "arm64"]
log("Hardware arch: \(isArm64 ? "arm64" : "x86_64")")

// ── Find a Python with our deps ───────────────────────────────────────────────
let pythonCandidates = [
    "\(nolanRoot)/.venv/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
]

func probePython(_ path: String, arch: String) -> Bool {
    guard FileManager.default.isExecutableFile(atPath: path) else { return false }
    let p = Process()
    p.launchPath = "/usr/bin/arch"
    p.arguments = ["-\(arch)", path, "-c", "import dotenv, fastapi, faster_whisper"]
    let null = Pipe()
    p.standardOutput = null
    p.standardError = null
    do { try p.run() } catch { return false }
    p.waitUntilExit()
    return p.terminationStatus == 0
}

var chosenPython: String? = nil
var chosenArch: String? = nil
outer: for cand in pythonCandidates {
    for arch in archOrder {
        if probePython(cand, arch: arch) {
            chosenPython = cand
            chosenArch = arch
            log("Using: \(cand) as \(arch)")
            break outer
        }
    }
}

guard let python = chosenPython, let arch = chosenArch else {
    let alert = NSAlert()
    alert.messageText = "Nolan dependencies not installed"
    alert.informativeText =
        "Open Terminal and run:\n\n    cd \(nolanRoot) && ./install.sh\n\nThen re-launch Nolan."
    alert.alertStyle = .critical
    alert.runModal()
    exit(1)
}

// ── Kill any prior instance on port 8765 ──────────────────────────────────────
let (_, lsofOut) = runShell("/usr/sbin/lsof", ["-ti", ":8765"])
for pidStr in lsofOut.split(separator: "\n") {
    if let pid = Int32(pidStr) { kill(pid, SIGKILL) }
}

// ── Launch the Python server as a child process ───────────────────────────────
let serverProcess = Process()
serverProcess.launchPath = "/usr/bin/arch"
serverProcess.arguments = ["-\(arch)", python, "main.py"]
serverProcess.currentDirectoryURL = URL(fileURLWithPath: nolanRoot)
if let logHandle = FileHandle(forWritingAtPath: serverLog)
        ?? { FileManager.default.createFile(atPath: serverLog, contents: nil)
             return FileHandle(forWritingAtPath: serverLog) }() {
    serverProcess.standardOutput = logHandle
    serverProcess.standardError = logHandle
}
do { try serverProcess.run() } catch {
    log("Failed to start server: \(error)")
    exit(1)
}
log("Server pid: \(serverProcess.processIdentifier)")

// ── Wait for the server to become reachable, then open the browser ────────────
DispatchQueue.global(qos: .userInitiated).async {
    let url = URL(string: "http://localhost:8765/")!
    for _ in 0..<20 {
        if !serverProcess.isRunning { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.0
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        let task = URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let r = resp as? HTTPURLResponse, r.statusCode == 200 { ok = true }
            sem.signal()
        }
        task.resume()
        _ = sem.wait(timeout: .now() + 2)
        if ok { break }
        Thread.sleep(forTimeInterval: 0.7)
    }
    DispatchQueue.main.async {
        NSWorkspace.shared.open(url)
    }
}

// ── App delegate: clean shutdown ──────────────────────────────────────────────
class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Process

    init(server: Process) {
        self.server = server
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        log("NSApplication did finish launching — dock dot should be live")
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        log("Terminating: killing server pid \(server.processIdentifier)")
        if server.isRunning {
            kill(server.processIdentifier, SIGTERM)
            // Wait up to 4s for graceful exit
            let deadline = Date().addingTimeInterval(4)
            while server.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if server.isRunning { kill(server.processIdentifier, SIGKILL) }
        }
        return .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // We have no windows — keep running until explicitly quit.
        return false
    }
}

let app = NSApplication.shared
let delegate = AppDelegate(server: serverProcess)
app.delegate = delegate
app.setActivationPolicy(.regular)   // gives us the dock icon + dot
app.activate(ignoringOtherApps: false)

// Watch the server in the background — if it dies, quit the app.
DispatchQueue.global().async {
    serverProcess.waitUntilExit()
    log("Server exited with code \(serverProcess.terminationStatus); quitting app")
    DispatchQueue.main.async { NSApp.terminate(nil) }
}

app.run()
