// Host capability/validation evidence only. This never starts or installs a VM.
import Foundation
import Virtualization

var report: [String: Any] = [
    "schema_version": 1,
    "guest_execution_observed": false,
    "virtualization_supported": VZVirtualMachine.isSupported,
    "host_os": ProcessInfo.processInfo.operatingSystemVersionString,
    "generic_efi_is_xnu_boot_proof": false,
    "linux_loader_accepts_xnu_contract": false,
    "macos_custom_kernel_url_api": false
]
let configuration = VZVirtualMachineConfiguration()
configuration.platform = VZGenericPlatformConfiguration()
let loader = VZEFIBootLoader()
let variablesURL = FileManager.default.temporaryDirectory.appendingPathComponent("blauos-vz-probe-" + UUID().uuidString)
defer { try? FileManager.default.removeItem(at: variablesURL) }
loader.variableStore = try VZEFIVariableStore(creatingVariableStoreAt: variablesURL)
configuration.bootLoader = loader
configuration.cpuCount = 2
configuration.memorySize = max(VZVirtualMachineConfiguration.minimumAllowedMemorySize, 1024 * 1024 * 1024)
configuration.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: FileHandle.standardInput,
                                                     fileHandleForWriting: FileHandle.standardError)
configuration.serialPorts = [serial]
let network = VZVirtioNetworkDeviceConfiguration()
network.attachment = VZNATNetworkDeviceAttachment()
configuration.networkDevices = [network]
do {
    try configuration.validate()
    report["generic_efi_configuration_valid"] = true
} catch {
    report["generic_efi_configuration_valid"] = false
    report["validation_error"] = String(describing: error)
}
let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
print("")
