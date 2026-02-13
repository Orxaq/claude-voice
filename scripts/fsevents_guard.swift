#!/usr/bin/env swift
// fsevents_guard.swift — Anti-pause method #7
// Uses macOS FSEvents kernel API to watch the heartbeat file.
// If no FSEvent fires for 90 seconds, sends a nudge keystroke to Terminal.
// Rate-limited to 1 nudge per 60 seconds. No focus stealing.

import Foundation
import CoreServices

// MARK: - Configuration

let watchedFile = "/Users/sdevisch/.claude/autopilot/status.json"
let watchedDir  = (watchedFile as NSString).deletingLastPathComponent
let logPath     = "/Users/sdevisch/.claude/watchdogs/fsevents_guard.log"
let statusPath  = "/Users/sdevisch/.claude/watchdogs/fsevents_guard_status.json"

let staleSec:   Double = 90   // seconds without FSEvent before nudging
let cooldownSec: Double = 60  // minimum seconds between nudges

// MARK: - State

var lastEventTime   = Date()
var lastNudgeTime   = Date.distantPast
var totalNudges     = 0
var totalEvents     = 0
var running         = true
let startTime       = Date()

// MARK: - Logging

func log(_ msg: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "[\(ts)] \(msg)\n"
    if let fh = FileHandle(forWritingAtPath: logPath) {
        fh.seekToEndOfFile()
        fh.write(line.data(using: .utf8)!)
        fh.closeFile()
    } else {
        FileManager.default.createFile(atPath: logPath, contents: line.data(using: .utf8))
    }
    fputs(line, stderr)
}

// MARK: - Status writer

func writeStatus(extra: [String: Any] = [:]) {
    let now = Date()
    let age = now.timeIntervalSince(lastEventTime)
    var info: [String: Any] = [
        "pid": ProcessInfo.processInfo.processIdentifier,
        "method": "fsevents_guard",
        "method_number": 7,
        "started": ISO8601DateFormatter().string(from: startTime),
        "timestamp": ISO8601DateFormatter().string(from: now),
        "uptime_sec": round(now.timeIntervalSince(startTime) * 10) / 10,
        "last_fsevent": ISO8601DateFormatter().string(from: lastEventTime),
        "last_fsevent_age_sec": round(age * 10) / 10,
        "stale": age > staleSec,
        "last_nudge": lastNudgeTime == Date.distantPast ? "never" : ISO8601DateFormatter().string(from: lastNudgeTime),
        "total_nudges": totalNudges,
        "total_fsevents": totalEvents,
        "watched_file": watchedFile,
        "stale_threshold_sec": staleSec,
        "cooldown_sec": cooldownSec
    ]
    for (k, v) in extra { info[k] = v }

    if let data = try? JSONSerialization.data(withJSONObject: info, options: [.prettyPrinted, .sortedKeys]) {
        try? data.write(to: URL(fileURLWithPath: statusPath))
    }
}

// MARK: - Nudge via AppleScript

func sendNudge() {
    let now = Date()
    let sinceLast = now.timeIntervalSince(lastNudgeTime)
    guard sinceLast >= cooldownSec else {
        log("SKIP nudge — cooldown (\(Int(cooldownSec - sinceLast))s remaining)")
        return
    }

    // Send keystroke return to Terminal without activating/focusing
    let script = """
    tell application "System Events"
        tell process "Terminal"
            keystroke return
        end tell
    end tell
    """

    var error: NSDictionary?
    if let appleScript = NSAppleScript(source: script) {
        appleScript.executeAndReturnError(&error)
        if let err = error {
            log("NUDGE ERROR: \(err)")
        } else {
            lastNudgeTime = now
            totalNudges += 1
            log("NUDGE #\(totalNudges) sent (stale \(Int(now.timeIntervalSince(lastEventTime)))s, cooldown reset)")
        }
    } else {
        log("NUDGE FAILED: could not compile AppleScript")
    }
    writeStatus(extra: ["last_action": "nudge"])
}

// MARK: - FSEvents callback

let callback: FSEventStreamCallback = { (
    streamRef: ConstFSEventStreamRef,
    clientCallBackInfo: UnsafeMutableRawPointer?,
    numEvents: Int,
    eventPaths: UnsafeMutableRawPointer,
    eventFlags: UnsafePointer<FSEventStreamEventFlags>,
    eventIds: UnsafePointer<FSEventStreamEventId>
) in
    let paths = Unmanaged<CFArray>.fromOpaque(eventPaths).takeUnretainedValue() as! [String]
    for i in 0..<numEvents {
        let path = paths[i]
        // We watch the directory; filter for our specific file
        if path == watchedDir || path.hasSuffix("status.json") || path.contains("autopilot") {
            lastEventTime = Date()
            totalEvents += 1
            if totalEvents % 50 == 1 || totalEvents <= 5 {
                log("FSEvent #\(totalEvents) on: \(path) flags=\(eventFlags[i])")
            }
            writeStatus()
        }
    }
}

// MARK: - Signal handlers

func setupSignalHandlers() {
    signal(SIGTERM) { _ in
        log("Received SIGTERM, shutting down")
        running = false
        writeStatus(extra: ["state": "stopped"])
        exit(0)
    }
    signal(SIGINT) { _ in
        log("Received SIGINT, shutting down")
        running = false
        writeStatus(extra: ["state": "stopped"])
        exit(0)
    }
}

// MARK: - Main

func main() {
    setupSignalHandlers()

    // Ensure directories exist
    let fm = FileManager.default
    try? fm.createDirectory(atPath: (logPath as NSString).deletingLastPathComponent,
                           withIntermediateDirectories: true)
    try? fm.createDirectory(atPath: watchedDir,
                           withIntermediateDirectories: true)

    log("=== FSEvents Guard starting ===")
    log("Watching: \(watchedFile)")
    log("Stale threshold: \(Int(staleSec))s | Cooldown: \(Int(cooldownSec))s")
    log("PID: \(ProcessInfo.processInfo.processIdentifier)")

    // If the watched file already exists, set initial event time to its mtime
    if let attrs = try? fm.attributesOfItem(atPath: watchedFile),
       let mtime = attrs[.modificationDate] as? Date {
        lastEventTime = mtime
        log("Initial mtime: \(ISO8601DateFormatter().string(from: mtime))")
    }

    // Create FSEvent stream watching the directory containing status.json
    let pathsToWatch = [watchedDir] as CFArray
    var context = FSEventStreamContext(
        version: 0,
        info: nil,
        retain: nil,
        release: nil,
        copyDescription: nil
    )

    guard let stream = FSEventStreamCreate(
        kCFAllocatorDefault,
        callback,
        &context,
        pathsToWatch,
        FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
        1.0,  // latency: 1 second (coalesce events within 1s)
        UInt32(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagUseCFTypes)
    ) else {
        log("FATAL: Could not create FSEvent stream")
        exit(1)
    }

    // Use modern DispatchQueue-based scheduling (not deprecated RunLoop API)
    let eventQueue = DispatchQueue(label: "com.orxaq.fsevents-guard.events", qos: .utility)
    FSEventStreamSetDispatchQueue(stream, eventQueue)

    guard FSEventStreamStart(stream) else {
        log("FATAL: Could not start FSEvent stream")
        exit(1)
    }

    log("FSEvent stream active on: \(watchedDir)")
    writeStatus(extra: ["state": "running"])

    // Start the staleness checker timer on the main queue
    let staleTimer = DispatchSource.makeTimerSource(queue: .main)
    staleTimer.schedule(deadline: .now() + 15, repeating: 15.0)
    staleTimer.setEventHandler {
        let age = Date().timeIntervalSince(lastEventTime)
        if age > staleSec {
            log("STALE: no FSEvent for \(Int(age))s (threshold: \(Int(staleSec))s)")
            sendNudge()
        }
        writeStatus()
    }
    staleTimer.resume()

    log("Stale checker timer armed (15s interval)")
    log("=== FSEvents Guard running ===")

    // Block main thread forever (DispatchQueue handles events)
    dispatchMain()
}

main()
