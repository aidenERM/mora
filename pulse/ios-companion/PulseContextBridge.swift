import AppIntents
import CoreLocation
import EventKit
import HealthKit
import HomeKit
import MusicKit

/// Minimal native boundary. Pulse receives derived context, not raw personal datasets.
struct PulseContextPayload: Codable {
    let mode: String
    let confidence: Double
    let expiresAt: String?
    let signals: [String: [String: String]]
}

final class PulseContextBridge: NSObject, CLLocationManagerDelegate {
    private let eventStore = EKEventStore()
    private let healthStore = HKHealthStore()
    private let locationManager = CLLocationManager()
    private let homeManager = HMHomeManager()

    func requestSelectedPermissions() async throws {
        try await eventStore.requestFullAccessToEvents()
        try await eventStore.requestFullAccessToReminders()
        if HKHealthStore.isHealthDataAvailable() {
            let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!
            try await healthStore.requestAuthorization(toShare: [], read: [sleep])
        }
        locationManager.requestWhenInUseAuthorization()
        // HomeKit and MusicKit are requested only when the user enables those cards.
    }

    /// Convert local signals into a small, expiring payload before uploading.
    func derivedPayload(mode: String, confidence: Double, expiresAt: String?) -> PulseContextPayload {
        PulseContextPayload(mode: mode, confidence: min(1, max(0, confidence)), expiresAt: expiresAt, signals: [:])
    }
}
