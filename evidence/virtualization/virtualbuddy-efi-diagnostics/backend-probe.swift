import Foundation
import Virtualization
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let disk = URL(fileURLWithPath: CommandLine.arguments[2])
let fm = FileManager.default
try fm.createDirectory(at: output, withIntermediateDirectories: false)
var report: [String: Any] = [
    "schema_version": 1,
    "backend": "Virtualization.framework generic EFI",
    "virtualbuddy_app_started_vm": false,
    "framework_start_succeeded": false,
    "independent_xnu_execution_observed": false,
    "physical_gate_passed": false,
    "host_os": ProcessInfo.processInfo.operatingSystemVersionString,
    "disk": disk.path,
    "events": [[String: Any]](),
    "screenshot_method": "Public AppKit cacheDisplay of the owned VZVirtualMachineView; no host desktop capture",
    "configuration": "VirtualBuddy 2.1 generic EFI, 1 CPU, 1 GiB, virtio block/GPU/network, USB input, empty virtiofs and SPICE console; no extra serial port"
]
func event(_ name: String, _ extra: [String: Any] = [:]) {
    var e = extra
    e["name"] = name
    e["time"] = ISO8601DateFormatter().string(from: Date())
    var events = report["events"] as! [[String: Any]]
    events.append(e)
    report["events"] = events
    try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        .write(to: output.appendingPathComponent("result.json"), options: .atomic)
    print(name, extra)
    fflush(stdout)
}
let config = VZVirtualMachineConfiguration()
config.platform = VZGenericPlatformConfiguration()
let boot = VZEFIBootLoader()
boot.variableStore = try VZEFIVariableStore(creatingVariableStoreAt: output.appendingPathComponent("nvram"))
config.bootLoader = boot
config.cpuCount = 1
config.memorySize = 1024 * 1024 * 1024
let storage = try VZDiskImageStorageDeviceAttachment(url: disk, readOnly: false,
    cachingMode: .cached, synchronizationMode: .fsync)
config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: storage)]
let graphics = VZVirtioGraphicsDeviceConfiguration()
graphics.scanouts = [VZVirtioGraphicsScanoutConfiguration(widthInPixels: 1280, heightInPixels: 800)]
config.graphicsDevices = [graphics]
config.keyboards = [VZUSBKeyboardConfiguration()]
config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
let network = VZVirtioNetworkDeviceConfiguration()
network.attachment = VZNATNetworkDeviceAttachment()
network.macAddress = VZMACAddress(string: "02:42:B1:A0:05:02")!
config.networkDevices = [network]
let sharing = VZVirtioFileSystemDeviceConfiguration(tag: "VirtualBuddyShared")
sharing.share = VZMultipleDirectoryShare(directories: [:])
config.directorySharingDevices = [sharing]
let spice = VZVirtioConsoleDeviceConfiguration()
let spicePort = VZVirtioConsolePortConfiguration()
spicePort.name = VZSpiceAgentPortAttachment.spiceAgentPortName
spicePort.attachment = VZSpiceAgentPortAttachment()
spice.ports[0] = spicePort
config.consoleDevices = [spice]
try config.validate()
event("configuration-validated")
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let vm = VZVirtualMachine(configuration: config)
let view = VZVirtualMachineView(frame: NSRect(x: 0, y: 0, width: 1280, height: 800))
view.virtualMachine = vm
let window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
window.title = "BlauOS EFI trace - bounded backend probe"
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

final class Delegate: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ virtualMachine: VZVirtualMachine) { event("guest-stopped") }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        event("vm-stopped-with-error", ["error": String(describing: error)])
    }
}
let delegate = Delegate()
vm.delegate = delegate
func screenshot(_ name: String) {
    guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
        event("screenshot-unavailable")
        return
    }
    view.cacheDisplay(in: view.bounds, to: bitmap)
    if let data = bitmap.representation(using: .png, properties: [:]) {
        do {
            try data.write(to: output.appendingPathComponent(name + ".png"))
            event("screenshot-saved", ["file": name + ".png", "bytes": data.count])
        } catch { event("screenshot-write-failed", ["error": String(describing: error)]) }
    }
}

DispatchQueue.main.async {
    vm.start { result in
        switch result {
        case .success:
            report["framework_start_succeeded"] = true
            event("framework-start-succeeded")
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { screenshot("frame-4s") }
            DispatchQueue.main.asyncAfter(deadline: .now() + 12) { screenshot("frame-12s") }
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
                event("bounded-observation-finished", ["vm_state": vm.state.rawValue, "serial_bytes": 0])
                if vm.canStop {
                    vm.stop { r in
                        event("host-stopped-probe", ["result": String(describing: r)])
                        exit(0)
                    }
                } else { exit(0) }
            }
        case .failure(let error):
            event("framework-start-failed", ["error": String(describing: error)])
            exit(2)
        }
    }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 35) {
    event("probe-watchdog-expired")
    exit(3)
}
app.run()
