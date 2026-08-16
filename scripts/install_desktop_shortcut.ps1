param(
    [string]$ShortcutPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot 'scripts\launch_control_room.ps1'
$icon = Join-Path $projectRoot 'src\peerbridge_mcp\release_support\peerbridge-icon.ico'
$appUserModelId = 'PeerBridge.MCP.ControlRoom'
if (-not $ShortcutPath) {
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PeerBridge MCP Control Room.lnk'
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Icon not found: $icon"
}

if (-not ('PeerBridge.WindowsShortcutIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace PeerBridge
{
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    internal struct PropertyKey
    {
        internal Guid FormatId;
        internal uint PropertyId;

        internal PropertyKey(Guid formatId, uint propertyId)
        {
            FormatId = formatId;
            PropertyId = propertyId;
        }
    }

    [StructLayout(LayoutKind.Explicit)]
    internal struct PropVariant
    {
        [FieldOffset(0)] internal ushort VariantType;
        [FieldOffset(8)] internal IntPtr PointerValue;

        internal static PropVariant FromString(string value)
        {
            return new PropVariant
            {
                VariantType = 31, // VT_LPWSTR
                PointerValue = Marshal.StringToCoTaskMemUni(value)
            };
        }
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint propertyCount);
        [PreserveSig] int GetAt(uint propertyIndex, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    public static class WindowsShortcutIdentity
    {
        private const uint ReadWrite = 0x00000002;
        private const uint AppUserModelIdPropertyId = 5;
        private static readonly Guid AppUserModelFormatId =
            new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int SHGetPropertyStoreFromParsingName(
            string path,
            IntPtr bindContext,
            uint flags,
            ref Guid interfaceId,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore propertyStore);

        [DllImport("ole32.dll", PreserveSig = true)]
        private static extern int PropVariantClear(ref PropVariant value);

        public static void SetAppUserModelId(string shortcutPath, string appUserModelId)
        {
            Guid interfaceId = typeof(IPropertyStore).GUID;
            IPropertyStore propertyStore;
            Marshal.ThrowExceptionForHR(
                SHGetPropertyStoreFromParsingName(
                    shortcutPath,
                    IntPtr.Zero,
                    ReadWrite,
                    ref interfaceId,
                    out propertyStore));

            PropertyKey key = new PropertyKey(AppUserModelFormatId, AppUserModelIdPropertyId);
            PropVariant value = PropVariant.FromString(appUserModelId);
            try
            {
                Marshal.ThrowExceptionForHR(propertyStore.SetValue(ref key, ref value));
                Marshal.ThrowExceptionForHR(propertyStore.Commit());
            }
            finally
            {
                PropVariantClear(ref value);
                if (propertyStore != null && Marshal.IsComObject(propertyStore))
                {
                    Marshal.FinalReleaseComObject(propertyStore);
                }
            }
        }
    }
}
'@
}

$shell = New-Object -ComObject WScript.Shell
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'PeerBridge MCP Control Room.lnk'
$shortcutPaths = @($ShortcutPath, $startMenuShortcut) | Select-Object -Unique

foreach ($path in $shortcutPaths) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = 'Launch the PeerBridge MCP Control Room'
    $shortcut.Save()
    [PeerBridge.WindowsShortcutIdentity]::SetAppUserModelId($path, $appUserModelId)
}

$registrationPath = "HKCU:\Software\Classes\AppUserModelId\$appUserModelId"
New-Item -Path $registrationPath -Force | Out-Null
New-ItemProperty -Path $registrationPath -Name 'DisplayName' -Value 'PeerBridge MCP Control Room' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registrationPath -Name 'IconUri' -Value $icon -PropertyType String -Force | Out-Null

[pscustomobject]@{
    Shortcut = (Resolve-Path -LiteralPath $ShortcutPath).Path
    StartMenuShortcut = (Resolve-Path -LiteralPath $startMenuShortcut).Path
    Icon = (Resolve-Path -LiteralPath $icon).Path
    Target = $shortcut.TargetPath
    AppUserModelId = $appUserModelId
} | ConvertTo-Json
