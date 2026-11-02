"""Managed IO must accept SDK paths beyond MAX_PATH in the installer stage."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"


@pytest.mark.skipif(os.name != "nt" or not CSC.is_file(), reason="Windows .NET Framework required")
def test_production_installer_path_io_writes_reads_and_removes_long_owned_fixture(tmp_path):
    driver = tmp_path / "LongPathFixture.cs"
    driver.write_text(r'''
using System;
using System.Collections.Generic;
using System.IO;
using CodeAgent.Installer;
internal static class LongPathFixture {
 static int Main(string[] args) {
  try {
   InstallCommon.EnableLongPaths();
   string root=Path.GetFullPath(args[0]);
   string relative=new String('a',90)+"/"+new String('b',90)+"/"+new String('c',90)+"/fixture.txt";
   string path=InstallCommon.FilePath(root,relative);
   if(path.Length<=300)throw new Exception("The fixture must cross MAX_PATH");
   Directory.CreateDirectory(Path.GetDirectoryName(path));
   using(FileStream stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None))
   using(StreamWriter writer=new StreamWriter(stream))writer.Write("portable SDK path verified");
   if(File.ReadAllText(path)!="portable SDK path verified")throw new Exception("Content mismatch");
   // Exercise the production uninstall path too, with an explicitly owned
   // file list. No registration, shortcut or user application is touched.
   InstallCommon.RemoveOwnedFiles(root,new Dictionary<string,object>{{"files",new string[]{relative}}});
   if(File.Exists(path)||Directory.Exists(root))throw new Exception("Owned fixture removal failed");
   Console.WriteLine("PASS long path length="+path.Length);return 0;
  } catch(Exception e) {Console.WriteLine(e.ToString());return 9;}
 }
}
''', encoding="utf-8")
    executable = tmp_path / "LongPathFixture.exe"
    compiled = subprocess.run([str(CSC), "/nologo", "/utf8output", "/target:exe", "/platform:x64", "/main:LongPathFixture",
                               f"/out:{executable}", f"/win32manifest:{ROOT / 'desktop/installer/app.manifest'}",
                               "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Windows.Forms.dll",
                               str(driver), str(ROOT / "desktop/installer/Uninstall.cs")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    owned = tmp_path / "owned isolated install"
    result = subprocess.run([str(executable), str(owned)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0 and "PASS long path" in result.stdout, result.stdout + result.stderr
