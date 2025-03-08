# Adapted from: https://github.com/Blinue/Magpie/blob/d9f878075f399c4ab7fec3cc856f98dfdc96e163/publish.py

import ctypes, sys
from ctypes import windll, wintypes
import uuid
import subprocess
import os
import glob
import shutil
from xml.etree import ElementTree
import requests
from tqdm import tqdm
import urllib.request
import zipfile
import time

try:
    # https://docs.github.com/en/actions/learn-github-actions/variables
    if os.environ["GITHUB_ACTIONS"].lower() == "true":
        # 不知为何在 Github Actions 中运行时默认编码为 ANSI，并且 print 需刷新流才能正常显示
        for stream in [sys.stdout, sys.stderr]:
            stream.reconfigure(encoding='utf-8')
except:
    pass

#####################################################################
#
# 使用 vswhere 查找 msbuild
#
#####################################################################

class FOLDERID:
    ProgramFilesX86 = uuid.UUID('{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}')

# 包装 SHGetKnownFolderPath，来自 https://gist.github.com/mkropat/7550097
def get_known_folder_path(folderid):
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8)
        ]

        def __init__(self, uuid_):
            ctypes.Structure.__init__(self)
            self.Data1, self.Data2, self.Data3, self.Data4[0], self.Data4[1], rest = uuid_.fields
            for i in range(2, 8):
                self.Data4[i] = rest>>(8 - i - 1)*8 & 0xff

    CoTaskMemFree = windll.ole32.CoTaskMemFree
    CoTaskMemFree.restype = None
    CoTaskMemFree.argtypes = [ctypes.c_void_p]

    SHGetKnownFolderPath = windll.shell32.SHGetKnownFolderPath
    SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p)
    ]

    fid = GUID(folderid) 
    pPath = ctypes.c_wchar_p()
    if SHGetKnownFolderPath(ctypes.byref(fid), 0, wintypes.HANDLE(0), ctypes.byref(pPath)) != 0:
        raise FileNotFoundError()
    path = pPath.value
    CoTaskMemFree(pPath)
    return path

programFilesX86Path = get_known_folder_path(FOLDERID.ProgramFilesX86);

vswherePath = programFilesX86Path + "\\Microsoft Visual Studio\\Installer\\vswhere.exe"
if not os.access(vswherePath, os.X_OK):
    raise Exception("未找到 vswhere")

process = subprocess.run(vswherePath + " -latest -requires Microsoft.Component.MSBuild -find MSBuild\\**\\Bin\\MSBuild.exe", capture_output=True)
msbuildPath = str(process.stdout, encoding="utf-8").splitlines()[0]
if not os.access(msbuildPath, os.X_OK):
    raise Exception("未找到 msbuild")

# Download NuGet package

class DownloadProgressBar(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)

def download_url(url, output_path):
    with DownloadProgressBar(unit='B', unit_scale=True,
                             miniters=1, desc=url.split('/')[-1]) as t:
        urllib.request.urlretrieve(url, filename=output_path, reporthook=t.update_to)

print("正在请求 NuGet...")
r = requests.get("https://api.nuget.org/v3-flatcontainer/Microsoft.UI.Xaml/index.json")
if r.status_code == 200:
    muxVersions = requests.get("https://api.nuget.org/v3-flatcontainer/Microsoft.UI.Xaml/index.json").json()["versions"]
    print("Microsoft.UI.Xaml 具有如下版本：")
    print(muxVersions)
else:
    muxVersions = ["2.8.7-prerelease.241119001"]
    print("无法访问 NuGet。使用默认值。")
nugetVersionToDownload = input(f"输入要下载的 Microsoft.UI.Xaml 版本([{muxVersions[-1]}])：")
if nugetVersionToDownload == "":
    nugetVersionToDownload = muxVersions[-1]
print(f"已选择 {nugetVersionToDownload}。")

shutil.rmtree("out", ignore_errors=True)
os.makedirs("out", exist_ok=True)

download_url(f"https://www.nuget.org/api/v2/package/Microsoft.UI.Xaml/{nugetVersionToDownload}", "out/input_mux.nupkg")

print("下载完成。")

print("** 正在进入目录 ./out")
os.chdir("./out")

rootDir = os.getcwd()

#####################################################################
#
# 修剪 resources.pri
# 参考自 https://github.com/microsoft/microsoft-ui-xaml/pull/4400
#
#####################################################################

def trim_resources_pri():
    # 取最新的 Windows SDK
    windowsSdkDir = sorted(glob.glob(programFilesX86Path + "\\Windows Kits\\10\\bin\\10.*"))[-1];
    makepriPath = windowsSdkDir + "\\x64\\makepri.exe"
    if not os.access(makepriPath, os.X_OK):
        raise Exception("未找到 makepri")

    # 将 resources.pri 的内容导出为 xml
    if os.system("\"" + makepriPath + "\" dump /dt detailed /o") != 0:
        raise Exception("dump 失败")

    xmlTree = ElementTree.parse("resources.pri.xml")

    # 在 xml 中删除冗余资源
    for resourceNode in xmlTree.getroot().findall("ResourceMap/ResourceMapSubtree/ResourceMapSubtree/ResourceMapSubtree/NamedResource"):
        name = resourceNode.get("name")

        if not name.endswith(".xbf"):
            continue

        # 我们仅需 19h1 和 21h1 的资源，分别用于 Win10 和 Win11
        for key in ["compact", "Compact", "v1", "rs2", "rs3", "rs4", "rs5"]:
            if key in name:
                # 将文件内容替换为一个空格（Base64 为 "IA=="）
                resourceNode.find("Candidate/Base64Value").text = "IA=="
                break

    xmlTree.write("resources.pri.xml", encoding="utf-8")

    with open("priconfig.xml", "w", encoding="utf-8") as f:
        print("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <resources targetOsVersion="10.0.0" majorVersion="1">
    <packaging>
        <autoResourcePackage qualifier="Scale" />
        <autoResourcePackage qualifier="DXFeatureLevel" />
    </packaging>
    <index startIndexAt="resources.pri.xml" root="">
        <default>
        <qualifier name="Language" value="en-US" />
        <qualifier name="Contrast" value="standard" />
        <qualifier name="Scale" value="200" />
        <qualifier name="HomeRegion" value="001" />
        <qualifier name="TargetSize" value="256" />
        <qualifier name="LayoutDirection" value="LTR" />
        <qualifier name="DXFeatureLevel" value="DX9" />
        <qualifier name="Configuration" value="" />
        <qualifier name="AlternateForm" value="" />
        <qualifier name="Platform" value="UAP" />
        </default>
        <indexer-config type="priinfo" emitStrings="true" emitPaths="true" emitEmbeddedData="true" />
    </index>
    </resources>""", file=f)

    # 将 xml 重新封装成 pri
    os.system("\"" + makepriPath + "\" new /pr . /cf priconfig.xml /in Microsoft.UI.Xaml /o")

    os.remove("resources.pri.xml")
    os.remove("priconfig.xml")

with zipfile.ZipFile("input_mux.nupkg", 'r') as zip_ref:
    for info in zip_ref.infolist():
        extracted_path = zip_ref.extract(info, ".")
        date_time = info.date_time
        date_time = time.mktime(date_time + (0, 0, -1))
        os.utime(extracted_path, (date_time, date_time))

# 修剪资源
print("正在修剪资源...")

for root, dirs, files in os.walk("."):
    for file in files:
        if file == "Microsoft.UI.Xaml.pri":
            print(f"修剪 {os.path.join(root, file)}", flush=True)
            os.chdir(root)
            os.rename("Microsoft.UI.Xaml.pri", "resources.pri")
            fileTime = os.stat("resources.pri").st_mtime
            trim_resources_pri()
            os.utime("resources.pri", (fileTime, fileTime))
            os.rename("resources.pri", "Microsoft.UI.Xaml.pri")
            os.chdir(rootDir)

print("已修剪 resources.pri", flush=True)

# Modify version tag (without changing modified time)
nuspecContent = ""
nuspecTime = os.stat("Microsoft.UI.Xaml.nuspec").st_mtime
with open("Microsoft.UI.Xaml.nuspec", "r", encoding="utf-8") as f:
    nuspecContent = f.read()
nuspecContent = nuspecContent.replace(f"<version>{nugetVersionToDownload}</version>",
                                      f"<version>{nugetVersionToDownload}.trim</version>")
with open("Microsoft.UI.Xaml.nuspec", "w", encoding="utf-8") as f:
    f.write(nuspecContent)
os.utime("Microsoft.UI.Xaml.nuspec", (nuspecTime, nuspecTime))

# os.remove("input_mux.nupkg")

# 重新打包为 zip
output_file = f"Microsoft.UI.Xaml.{nugetVersionToDownload}.trim.nupkg"
with zipfile.ZipFile(output_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
    for root, dirs, files in os.walk("."):
        for file in files:
            if file.endswith(".nupkg") or file == ".signature.p7s":
                continue
            file_path = os.path.join(root, file)
            arcname = os.path.relpath(file_path, ".")
            
            # 获取文件原始时间戳
            file_info = zipfile.ZipInfo(arcname)
            file_stat = os.stat(file_path)
            # 将文件的mtime转换为zip需要的格式 (年, 月, 日, 时, 分, 秒)
            date_time = time.localtime(file_stat.st_mtime)
            file_info.date_time = date_time
            
            # 写入文件并保留压缩方式
            with open(file_path, 'rb') as f:
                zipf.writestr(file_info, f.read(), zipfile.ZIP_DEFLATED)

print(f"已生成精简版 NuGet 包: {output_file}", flush=True)
