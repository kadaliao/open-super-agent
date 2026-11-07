// swift-tools-version:5.9
// App 的原生部分：推送注册和钥匙、弹出页、分享面板、角标、点通知。由 ../build.rs 编进 App。
import PackageDescription

let package = Package(
    name: "tauri-plugin-osa-ios",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "tauri-plugin-osa-ios", type: .static, targets: ["tauri-plugin-osa-ios"])],
    dependencies: [
        .package(name: "Tauri", path: "../.tauri/tauri-api"),
        .package(name: "WebPushCrypto", path: "../../swift/WebPushCrypto"),
    ],
    targets: [
        .target(name: "tauri-plugin-osa-ios", dependencies: [
            .byName(name: "Tauri"),
            .product(name: "WebPushCrypto", package: "WebPushCrypto"),
        ], path: "Sources")
    ]
)
