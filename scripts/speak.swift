#!/usr/bin/env swift
// speak.swift — High-quality TTS using AVSpeechSynthesizer (Apple Neural voices)
//
// Usage:
//   speak "Hello world"
//   speak --voice com.apple.voice.premium.en-US.Zoe "Hello world"
//   speak --list   (list available voices)
//   speak --rate 0.5 "Slower speech"
//
// AVSpeechSynthesizer gives access to premium/neural voices that
// the `say` command cannot use.

import AVFoundation
import Foundation

// Parse arguments
var args = Array(CommandLine.arguments.dropFirst())
var voiceId: String? = nil
var rate: Float = AVSpeechUtteranceDefaultSpeechRate
var listVoices = false
var text = ""

var i = 0
while i < args.count {
    switch args[i] {
    case "--voice", "-v":
        i += 1
        if i < args.count { voiceId = args[i] }
    case "--rate", "-r":
        i += 1
        if i < args.count { rate = Float(args[i]) ?? rate }
    case "--list", "-l":
        listVoices = true
    case "--help", "-h":
        print("Usage: speak [--voice ID] [--rate 0.0-1.0] [--list] TEXT")
        print("  --voice ID   AVSpeechSynthesisVoice identifier")
        print("  --rate N     Speech rate (0.0=slowest, 1.0=fastest, default=0.5)")
        print("  --list       List available voices")
        exit(0)
    default:
        if text.isEmpty {
            text = args[i]
        } else {
            text += " " + args[i]
        }
    }
    i += 1
}

if listVoices {
    let voices = AVSpeechSynthesisVoice.speechVoices()
    let english = voices.filter { $0.language.hasPrefix("en") }
    for v in english.sorted(by: { $0.identifier < $1.identifier }) {
        let quality: String
        switch v.quality {
        case .enhanced: quality = "Enhanced"
        case .premium: quality = "Premium"
        default: quality = "Default"
        }
        print("\(v.identifier)  \(v.name)  \(v.language)  \(quality)")
    }
    exit(0)
}

guard !text.isEmpty else {
    print("Error: No text provided")
    exit(1)
}

let synthesizer = AVSpeechSynthesizer()
let utterance = AVSpeechUtterance(string: text)
utterance.rate = rate

// Try to find the best voice
if let vid = voiceId, let voice = AVSpeechSynthesisVoice(identifier: vid) {
    utterance.voice = voice
} else {
    // Try premium voices first, then enhanced, then default
    let voices = AVSpeechSynthesisVoice.speechVoices()
        .filter { $0.language.hasPrefix("en-US") }
        .sorted { v1, v2 in
            let q1 = v1.quality == .premium ? 3 : v1.quality == .enhanced ? 2 : 1
            let q2 = v2.quality == .premium ? 3 : v2.quality == .enhanced ? 2 : 1
            return q1 > q2
        }

    if let best = voices.first {
        utterance.voice = best
    }
}

// Delegate to detect when speech finishes
class SpeechDelegate: NSObject, AVSpeechSynthesizerDelegate {
    let semaphore = DispatchSemaphore(value: 0)

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        semaphore.signal()
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        semaphore.signal()
    }
}

let delegate = SpeechDelegate()
synthesizer.delegate = delegate
synthesizer.speak(utterance)
delegate.semaphore.wait()
