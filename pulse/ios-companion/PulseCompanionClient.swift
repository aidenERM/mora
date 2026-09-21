import Foundation
import Security

/// Small authenticated client used by the native companion after pairing.
final class PulseCompanionClient {
    private let baseURL: URL
    private let keychainKey = "pulse.companion.bearer"

    init(baseURL: URL = URL(string: "https://pulse.moralife.uk")!) {
        self.baseURL = baseURL
    }

    func redeem(code: String, label: String) async throws {
        let token = try await request(path: "/api/companion/pair/redeem", body: ["code": code, "label": label])
        guard let value = token["token"] as? String else { throw URLError(.cannotParseResponse) }
        try KeychainToken.save(value, key: keychainKey)
    }

    func upload(payload: PulseContextPayload) async throws {
        guard let token = try KeychainToken.load(key: keychainKey) else { throw URLError(.userAuthenticationRequired) }
        var request = URLRequest(url: baseURL.appendingPathComponent("api/companion/context"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONEncoder().encode(payload)
        let (_, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
    }

    private func request(path: String, body: [String: String]) async throws -> [String: Any] {
        var request = URLRequest(url: baseURL.appendingPathComponent(path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }
}

private enum KeychainToken {
    static func save(_ value: String, key: String) throws {
        let data = Data(value.utf8)
        SecItemDelete([kSecClass: kSecClassGenericPassword, kSecAttrAccount: key] as CFDictionary)
        let status = SecItemAdd([kSecClass: kSecClassGenericPassword, kSecAttrAccount: key, kSecValueData: data] as CFDictionary, nil)
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }

    static func load(key: String) throws -> String? {
        var result: AnyObject?
        let status = SecItemCopyMatching([kSecClass: kSecClassGenericPassword, kSecAttrAccount: key, kSecReturnData: true] as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        return String(data: data, encoding: .utf8)
    }
}
