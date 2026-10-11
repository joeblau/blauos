import Foundation
import Virtualization
import AppKit

// Bounded backend probe for the generic EFI console completion work
// (issues #101 and #102). Unlike virtualbuddy_probe.swift, this probe:
//  - attaches the guest serial port's read side to a host FIFO so typed
//    characters reach the guest console input path (virtio console RX), and
//  - optionally issues the public requestStop after a fixed delay and then
//    requires the public guestDidStop callback within a bounded grace window,
//    recording the host force-stop recovery action if orderly shutdown fails.
// It never claims physical Apple Silicon results.

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let disk = URL(fileURLWithPath: CommandLine.arguments[2])
let fm = FileManager.default
func optionSeconds(_ name: String, _ fallback: Double, _ range: ClosedRange<Double>) -> Double {
    guard CommandLine.arguments.contains(name) else { return fallback }
    guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count,
          let value = Double(CommandLine.arguments[index + 1]), range.contains(value) else {
        fatalError("Bounded \(name) must be within \(range)")
    }
    return value
}
let timeoutSeconds = optionSeconds("--timeout-seconds", 90, 15...600)
let requestStopAfter = optionSeconds("--request-stop-after", -1, 5...300)
let stopGrace = optionSeconds("--stop-grace-seconds", 30, 5...300)
try fm.createDirectory(at: output, withIntermediateDirectories: false)
var report: [String: Any] = [
    "schema_version": 1,
    "backend": "Virtualization.framework generic EFI",
    "probe_purpose": "console input injection and host-requested orderly shutdown",
    "virtualbuddy_app_started_vm": false,
    "framework_start_succeeded": false,
    "physical_gate_passed": false,
    "host_os": ProcessInfo.processInfo.operatingSystemVersionString,
    "disk": disk.path,
    "events": [[String: Any]](),
    "screenshot_method": "No screenshots",
    "configuration": "VirtualBuddy 2.1 generic EFI, 1 CPU, 1 GiB, virtio block/GPU/network, USB input, empty virtiofs and SPICE console",
    "extra_serial_port": !CommandLine.arguments.contains("--no-serial"),
    "console_input_fifo": "console-input.fifo",
    "request_stop_after_seconds": requestStopAfter,
    "stop_grace_seconds": stopGrace,
    "observation_seconds": timeoutSeconds
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
let serialURL = output.appendingPathComponent("serial.log")
fm.createFile(atPath: serialURL.path, contents: nil)
let serialOut = try FileHandle(forWritingTo: serialURL)
// Guest console input arrives through this FIFO; scripts/vm/console_input.py
// writes validated frames. O_RDWR keeps the attachment readable before and
// after writers come and go without injecting bytes on its own.
let fifoURL = output.appendingPathComponent("console-input.fifo")
if mkfifo(fifoURL.path, 0o600) != 0 { fatalError("console-input fifo: \(errno)") }
let fifoFD = open(fifoURL.path, O_RDWR | O_NONBLOCK)
guard fifoFD >= 0 else { fatalError("console-input fifo open: \(errno)") }
let serialIn = FileHandle(fileDescriptor: fifoFD, closeOnDealloc: true)
let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: serialIn, fileHandleForWriting: serialOut)
config.serialPorts = CommandLine.arguments.contains("--no-serial") ? [] : [serial]
try config.validate()
event("configuration-validated")
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let vm = VZVirtualMachine(configuration: config)
let view = VZVirtualMachineView(frame: NSRect(x: 0, y: 0, width: 1280, height: 800))
view.virtualMachine = vm
let window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
window.title = "BlauOS console completion - bounded backend probe"
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

var requestedStop = false
var guestStoppedAfterRequest = false
final class Delegate: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        event("guest-stopped", ["after_request_stop": requestedStop])
        exit(requestedStop ? 0 : 0)
    }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        event("vm-stopped-with-error", ["error": String(describing: error)])
    }
}
let delegate = Delegate()
vm.delegate = delegate

DispatchQueue.main.async {
    vm.start { result in
        switch result {
        case .success:
            report["framework_start_succeeded"] = true
            event("framework-start-succeeded")
            if requestStopAfter > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + requestStopAfter) {
                    requestedStop = true
                    event("host-request-stop", ["api": "VZVirtualMachine.requestStop"])
                    Task {
                        do {
                            try await vm.requestStop()
                        } catch {
                            event("request-stop-error", ["error": String(describing: error)])
                        }
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + stopGrace) {
                        if vm.state == .stopped { return }
                        event("orderly-shutdown-timeout", ["grace_seconds": stopGrace])
                        // Bounded recovery: the same force-stop an operator
                        // confirms in the VirtualBuddy window.
                        if vm.canStop {
                            vm.stop { r in
                                event("force-stop-recovery", ["result": String(describing: r)])
                                exit(4)
                            }
                        } else { event("force-stop-unavailable"); exit(4) }
                    }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + timeoutSeconds) {
                event("bounded-observation-finished", ["vm_state": vm.state.rawValue, "serial_bytes": (try? Data(contentsOf: serialURL).count) ?? 0])
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
DispatchQueue.main.asyncAfter(deadline: .now() + timeoutSeconds + stopGrace + 30) {
    event("probe-watchdog-expired")
    exit(3)
}
app.run()
