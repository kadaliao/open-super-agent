import CryptoKit
import Foundation

/// 网页推送的接收方（RFC 8291，aes128gcm）：服务器（osa/push.py）用这台手机的公钥加密，只有手机自己的私钥能解开。
public enum WebPushError: Error, Equatable {
    case malformed      // 不是 aes128gcm 格式
    case decryptFailed  // 钥匙不对或者被改过
    case noKeys         // 这台手机上没有这个订阅的钥匙
}

/// 一个订阅的钥匙：P-256 私钥和 16 字节的 auth。公钥（65 字节，未压缩）交给服务器订阅。
public struct PushKeys {
    public let privateKey: P256.KeyAgreement.PrivateKey
    public let auth: Data

    public init(privateKey: P256.KeyAgreement.PrivateKey, auth: Data) {
        self.privateKey = privateKey
        self.auth = auth
    }

    /// 新生成一对
    public static func generate() -> PushKeys {
        var auth = Data(count: 16)
        _ = auth.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 16, $0.baseAddress!) }
        return PushKeys(privateKey: P256.KeyAgreement.PrivateKey(), auth: auth)
    }

    public var publicKey: Data { privateKey.publicKey.x963Representation }
}

public enum Aes128gcm {
    /// 解开一条推送，返回明文（去掉了结尾的分隔符和填充）。
    public static func decrypt(_ body: Data, keys: PushKeys) throws -> Data {
        let b = [UInt8](body)
        // 头：16 字节 salt、4 字节记录大小、1 字节公钥长度、服务器的临时公钥；后面是密文和 16 字节的校验
        guard b.count >= 21 + 65 + 17, b[20] == 65 else { throw WebPushError.malformed }
        let salt = Data(b[0..<16])
        let recordSize = Int(b[16]) << 24 | Int(b[17]) << 16 | Int(b[18]) << 8 | Int(b[19])
        let serverPublic = Data(b[21..<86])
        let sealed = Data(b[86...])
        guard recordSize >= 18, sealed.count <= recordSize else { throw WebPushError.malformed }   // 只认单条记录

        let shared: SharedSecret
        do {
            let serverKey = try P256.KeyAgreement.PublicKey(x963Representation: serverPublic)
            shared = try keys.privateKey.sharedSecretFromKeyAgreement(with: serverKey)
        } catch {
            throw WebPushError.malformed
        }
        let info = Data("WebPush: info\u{0}".utf8) + keys.publicKey + serverPublic
        let ikm = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: keys.auth, sharedInfo: info, outputByteCount: 32)
        let cek = HKDF<SHA256>.deriveKey(inputKeyMaterial: ikm, salt: salt, info: Data("Content-Encoding: aes128gcm\u{0}".utf8), outputByteCount: 16)
        let nonce = HKDF<SHA256>.deriveKey(inputKeyMaterial: ikm, salt: salt, info: Data("Content-Encoding: nonce\u{0}".utf8), outputByteCount: 12)

        let plain: Data
        do {
            let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: nonce.withUnsafeBytes { Data($0) }),
                                            ciphertext: sealed.dropLast(16), tag: sealed.suffix(16))
            plain = try AES.GCM.open(box, using: cek)
        } catch {
            throw WebPushError.decryptFailed
        }
        // 明文后面是分隔符 0x02（最后一条记录）和可选的 0 填充
        guard let end = plain.lastIndex(where: { $0 != 0 }), plain[end] == 2 else { throw WebPushError.malformed }
        return plain[plain.startIndex..<end]
    }
}
