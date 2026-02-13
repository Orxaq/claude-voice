// pause_monitor.swift
// Independent anti-pause monitor for Claude Code terminal sessions.
// Compiled with: swiftc -O -o pause_monitor pause_monitor.swift
// Uses GCD DispatchSource timer for efficient 15-second polling.

import Foundation
import Cocoa

// MARK: - Configuration

let heartbeatPath = NSString("~/.claude/autopilot/status.json").expandingTildeInPath
let logPath = NSString("~/.claude/watchdogs/pause_monitor.log").expandingTildeInPath
let statusPath = NSString("~/.claude/watchdogs/pause_monitor_status.json").expandingTildeInPath
let checkIntervalSeconds: Int = 15
let staleThresholdSeconds: Double = 120.0
let postKeystrokeDelay: UInt32 = 2  // seconds to wait between Enter and "continue"

// MARK: - Logging

func logMessage(_ message: String) {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let timestamp = formatter.string(from: Date())
    let line = "[\(timestamp)] \(message)\n"

    // Print to stdout for LaunchAgent capture
    print(line, terminator: "")

    // Append to log file
    let fileURL = URL(fileURLWithPath: logPath)
    if FileManager.default.fileExists(atPath: logPath) {
        if let handle = try? FileHandle(forWritingTo: fileURL) {
            handle.seekToEndOfFile()
            if let data = line.data(using: .utf8) {
                handle.write(data)
            }
            handle.closeFile()
        }
    } else {
        try? line.write(toFile: logPath, atomically: true, encoding: .utf8)
    }
}

// MARK: - Status Writer

struct MonitorStatus: Codable {
    let running: Bool
    let pid: Int32
    let lastCheck: String
    let lastHeartbeatAge: Double
    let interventionCount: Int
    let lastIntervention: String?
    let uptimeSeconds: Double

    enum CodingKeys: String, CodingKey {
        case running
        case pid
        case lastCheck = "last_check"
        case lastHeartbeatAge = "last_heartbeat_age_sec"
        case interventionCount = "intervention_count"
        case lastIntervention = "last_intervention"
        case uptimeSeconds = "uptime_seconds"
    }
}

var interventionCount = 0
var lastInterventionTimestamp: String? = nil
let startTime = Date()

func writeStatus(lastHeartbeatAge: Double) {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    let now = formatter.string(from: Date())
    let uptime = Date().timeIntervalSince(startTime)

    let status = MonitorStatus(
        running: true,
        pid: ProcessInfo.processInfo.processIdentifier,
        lastCheck: now,
        lastHeartbeatAge: lastHeartbeatAge,
        interventionCount: interventionCount,
        lastIntervention: lastInterventionTimestamp,
        uptimeSeconds: uptime
    )

    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? encoder.encode(status) {
        try? data.write(to: URL(fileURLWithPath: statusPath))
    }
}

// MARK: - Heartbeat Reader

struct HeartbeatData: Codable {
    let timestamp: String?
    let epoch: Double?
}

struct HeartbeatInfo: Codable {
    let state: String?
    let age_sec: Double?
    let data: HeartbeatData?
}

struct StatusFile: Codable {
    let timestamp: String?
    let heartbeat: HeartbeatInfo?
}

func readHeartbeatAge() -> Double? {
    guard FileManager.default.fileExists(atPath: heartbeatPath) else {
        logMessage("WARN: Heartbeat file not found at \(heartbeatPath)")
        return nil
    }

    guard let data = FileManager.default.contents(atPath: heartbeatPath) else {
        logMessage("WARN: Could not read heartbeat file")
        return nil
    }

    let decoder = JSONDecoder()
    guard let status = try? decoder.decode(StatusFile.self, from: data) else {
        logMessage("WARN: Could not parse heartbeat JSON")
        return nil
    }

    // Strategy 1: Use the pre-computed age_sec if available
    if let ageSec = status.heartbeat?.age_sec {
        // The age_sec was computed at status.timestamp time, so adjust
        // for time elapsed since status.timestamp was written.
        if let statusTimestamp = status.timestamp {
            let isoFormatter = ISO8601DateFormatter()
            isoFormatter.formatOptions = [.withInternetDateTime]
            if let statusDate = isoFormatter.date(from: statusTimestamp) {
                let elapsed = Date().timeIntervalSince(statusDate)
                return ageSec + elapsed
            }
        }
        return ageSec
    }

    // Strategy 2: Use the epoch directly
    if let epoch = status.heartbeat?.data?.epoch {
        return Date().timeIntervalSince1970 - epoch
    }

    // Strategy 3: Parse the heartbeat timestamp string
    if let tsString = status.heartbeat?.data?.timestamp {
        let isoFormatter = ISO8601DateFormatter()
        isoFormatter.formatOptions = [.withInternetDateTime]
        if let heartbeatDate = isoFormatter.date(from: tsString) {
            return Date().timeIntervalSince(heartbeatDate)
        }
    }

    logMessage("WARN: No usable timestamp found in heartbeat data")
    return nil
}

// MARK: - Intervention via AppleScript

func sendAntiPauseKeystroke() {
    logMessage("ACTION: Heartbeat stale — sending anti-pause intervention")

    // Step 1: Activate Terminal.app and send Enter
    let activateScript = """
    tell application "Terminal"
        activate
    end tell
    delay 0.5
    tell application "System Events"
        tell process "Terminal"
            set frontmost to true
        end tell
        keystroke return
    end tell
    """

    let appleScript1 = NSAppleScript(source: activateScript)
    var errorDict: NSDictionary? = nil
    appleScript1?.executeAndReturnError(&errorDict)

    if let error = errorDict {
        logMessage("ERROR: AppleScript activate/enter failed: \(error)")
        return
    }

    logMessage("ACTION: Sent Enter keystroke, waiting \(postKeystrokeDelay)s before sending 'continue'")

    // Step 2: Wait then send "continue" + Enter
    sleep(postKeystrokeDelay)

    let continueScript = """
    tell application "System Events"
        keystroke "continue"
        delay 0.3
        keystroke return
    end tell
    """

    let appleScript2 = NSAppleScript(source: continueScript)
    var errorDict2: NSDictionary? = nil
    appleScript2?.executeAndReturnError(&errorDict2)

    if let error2 = errorDict2 {
        logMessage("ERROR: AppleScript continue failed: \(error2)")
        return
    }

    // Record intervention
    interventionCount += 1
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    lastInterventionTimestamp = formatter.string(from: Date())

    logMessage("ACTION: Intervention #\(interventionCount) complete — sent 'continue' + Enter")
}

// MARK: - Main Check Cycle

func performCheck() {
    guard let age = readHeartbeatAge() else {
        writeStatus(lastHeartbeatAge: -1)
        return
    }

    let ageRounded = (age * 10).rounded() / 10

    if age > staleThresholdSeconds {
        logMessage("CHECK: Heartbeat age \(ageRounded)s exceeds threshold \(staleThresholdSeconds)s")
        sendAntiPauseKeystroke()
    } else {
        // Only log every ~60s when healthy to reduce noise
        let secondsSinceStart = Date().timeIntervalSince(startTime)
        let cycleCount = Int(secondsSinceStart) / checkIntervalSeconds
        if cycleCount % 4 == 0 {
            logMessage("OK: Heartbeat age \(ageRounded)s — within threshold")
        }
    }

    writeStatus(lastHeartbeatAge: ageRounded)
}

// MARK: - Entry Point

logMessage("START: pause_monitor v1.0 — PID \(ProcessInfo.processInfo.processIdentifier)")
logMessage("CONFIG: check_interval=\(checkIntervalSeconds)s, stale_threshold=\(staleThresholdSeconds)s")
logMessage("CONFIG: heartbeat=\(heartbeatPath)")
logMessage("CONFIG: log=\(logPath)")
logMessage("CONFIG: status=\(statusPath)")

// Ensure log directory exists
let watchdogDir = (logPath as NSString).deletingLastPathComponent
try? FileManager.default.createDirectory(atPath: watchdogDir, withIntermediateDirectories: true)

// Run initial check immediately
performCheck()

// Set up GCD timer for recurring checks
let queue = DispatchQueue(label: "com.orxaq.pause-monitor", qos: .utility)
let timer = DispatchSource.makeTimerSource(queue: queue)
timer.schedule(
    deadline: .now() + .seconds(checkIntervalSeconds),
    repeating: .seconds(checkIntervalSeconds),
    leeway: .seconds(1)
)
timer.setEventHandler {
    performCheck()
}
timer.resume()

logMessage("READY: GCD timer armed — monitoring every \(checkIntervalSeconds)s")

// Keep the process alive via RunLoop
dispatchMain()
