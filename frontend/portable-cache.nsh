!include FileFunc.nsh

!macro xiaoaCacheStart

  ; Build ID changes even when the application version is reused.
  StrCpy $INSTDIR "$EXEDIR\XiaoA-data\runtimes\app-${VERSION}-${UNPACK_DIR_NAME}"
  ClearErrors
  CreateDirectory $INSTDIR
  FileOpen $R1 "$INSTDIR\.write-test-$HWNDPARENT" w
  ${If} ${Errors}
    StrCpy $INSTDIR "$LOCALAPPDATA\xiaoa-portable\app-${VERSION}-${UNPACK_DIR_NAME}"
    ClearErrors
    CreateDirectory $INSTDIR
    FileOpen $R1 "$INSTDIR\.write-test-$HWNDPARENT" w
    ${If} ${Errors}
      MessageBox MB_ICONSTOP "Cannot create XiaoA runtime cache."
      SetErrorLevel 1
      Quit
    ${EndIf}
  ${EndIf}
  FileClose $R1
  Delete "$INSTDIR\.write-test-$HWNDPARENT"

  ; Serialize extraction for simultaneous double-clicks, then release before launch.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\XiaoA-cache-app-${VERSION}-${UNPACK_DIR_NAME}") p.r8'
  System::Call 'kernel32::WaitForSingleObject(p r8, i 60000) i.r9'
  ${If} $9 != 0
  ${AndIf} $9 != 128
    MessageBox MB_ICONSTOP "XiaoA runtime cache is busy. Please retry."
    SetErrorLevel 1
    Quit
  ${EndIf}
  IfFileExists "$INSTDIR\.xiaoa-ready" 0 xiaoa_extract
  IfFileExists "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0 xiaoa_extract
  IfFileExists "$INSTDIR\resources\app.asar" 0 xiaoa_extract
  IfFileExists "$INSTDIR\resources\backend\xiaoa-backend.exe" 0 xiaoa_extract
  IfFileExists "$INSTDIR\resources\backend\_internal\python312.dll" 0 xiaoa_extract
  IfFileExists "$INSTDIR\icudtl.dat" 0 xiaoa_extract
  Goto xiaoa_cached
xiaoa_extract:
  ; This path is always our build-specific cache, never the data/workspace root.
  RMDir /r $INSTDIR
  ClearErrors
  SetOutPath $INSTDIR
!macroend

!macro xiaoaCacheReady

  ${If} ${Errors}
    Delete "$INSTDIR\.xiaoa-ready"
    MessageBox MB_ICONSTOP "XiaoA runtime extraction failed. Please retry."
    SetErrorLevel 1
    Quit
  ${EndIf}
  ClearErrors
  FileOpen $R1 "$INSTDIR\.xiaoa-ready" w
  FileWrite $R1 "${VERSION}-${UNPACK_DIR_NAME}"
  FileClose $R1
  ${If} ${Errors}
    Delete "$INSTDIR\.xiaoa-ready"
    MessageBox MB_ICONSTOP "Cannot finalize XiaoA runtime cache. Please retry."
    SetErrorLevel 1
    Quit
  ${EndIf}
xiaoa_cached:
  System::Call 'kernel32::ReleaseMutex(p r8)'
  Call xiaoaCleanInactiveCaches
!macroend

; Each outer shell retains its mutex handle until Electron exits. Only an unused,
; marked cache directly beside the current build can be removed.
Function xiaoaCleanInactiveCaches
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  ${GetParent} "$INSTDIR" $0
  FindFirst $1 $2 "$0\app-*"
xiaoa_old_next:
  StrCmp $2 "" xiaoa_old_done
  StrCpy $3 "$0\$2"
  StrCmp $3 $INSTDIR xiaoa_old_skip
  IfFileExists "$3\.xiaoa-ready" 0 xiaoa_old_skip
  System::Call 'kernel32::GetFileAttributesW(w r3) i.r4'
  IntOp $4 $4 & 0x400
  IntCmp $4 0 0 xiaoa_old_skip xiaoa_old_skip
  ; A named mutex already present means a launcher/app still uses this build.
  System::Call 'kernel32::CreateMutexW(p 0, i 1, w "Local\XiaoA-cache-$2") p.r4 ?e'
  Pop $5
  StrCmp $4 0 xiaoa_old_skip
  StrCmp $5 183 xiaoa_old_close
  RMDir /r "$3"
  System::Call 'kernel32::ReleaseMutex(p r4)'
xiaoa_old_close:
  System::Call 'kernel32::CloseHandle(p r4)'
xiaoa_old_skip:
  FindNext $1 $2
  Goto xiaoa_old_next
xiaoa_old_done:
  FindClose $1
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
