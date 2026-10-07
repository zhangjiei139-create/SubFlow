import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs as NativeDialogs

ApplicationWindow {
    id: window
    width: 920
    height: 620
    minimumWidth: 800
    minimumHeight: 560
    visible: true
    color: "transparent"
    flags: Qt.Window | Qt.FramelessWindowHint
    title: "SubFlow v" + appVersion

    property color accent: "#07998d"
    property color accentDark: "#047d74"
    property color textPrimary: "#071012"
    property color textSecondary: "#2f3b40"
    property color canvasColor: "#f5f7f8"
    property string fontFamily: uiFontFamily
    property int currentPage: 0
    property int sourceIndex: 0
    property bool translationEnabled: true
    property bool audioExpanded: false
    property bool subtitleExpanded: false
    property bool logExpanded: true
    property var targetLanguages: ["英语"]
    property real batchLogSavedY: 0
    property bool batchLogFollowLatest: true

    function toggleTargetLanguage(language) {
        let values = targetLanguages.slice()
        let index = values.indexOf(language)
        if (index >= 0) {
            if (values.length === 1)
                return
            values.splice(index, 1)
        } else {
            values.push(language)
        }
        targetLanguages = values
    }

    function toggleMaximized() {
        if (window.visibility === Window.Maximized)
            window.showNormal()
        else
            window.showMaximized()
    }

    function capture(path) {
        surface.grabToImage(function(result) {
            result.saveToFile(path)
            Qt.quit()
        })
    }

    component AppText: Text {
        color: window.textPrimary
        font.family: window.fontFamily
        font.pixelSize: 12
        font.weight: Font.Medium
        font.hintingPreference: Font.PreferFullHinting
        renderType: Text.NativeRendering
    }

    component SoftCard: Rectangle {
        color: "#ffffff"
        radius: 12
        border.width: 1
        border.color: "#eef1f2"
        Rectangle {
            anchors.fill: parent
            anchors.topMargin: 4
            anchors.leftMargin: 2
            anchors.rightMargin: -2
            anchors.bottomMargin: -4
            radius: parent.radius
            color: "#120d403b"
            z: -1
        }
    }

    component PillButton: Rectangle {
        id: pill
        property alias text: pillText.text
        property bool selected: false
        signal clicked
        implicitHeight: 30
        implicitWidth: pillText.implicitWidth + 24
        radius: height / 2
        color: pillTap.pressed ? (selected ? "#036c64" : "#e7eeee") : selected ? window.accent : "#ffffff"
        border.width: 1
        border.color: selected ? window.accent : "#d9dfe1"
        scale: pillTap.pressed ? 0.96 : 1
        Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }
        AppText {
            id: pillText
            anchors.centerIn: parent
            color: pill.selected ? "#ffffff" : "#394247"
            font.pixelSize: 11
            font.weight: Font.Medium
        }
        TapHandler {
            id: pillTap
            onTapped: pill.clicked()
        }
    }

    component PresetButton: Rectangle {
        id: preset
        property alias text: presetText.text
        signal clicked
        implicitHeight: 30
        implicitWidth: presetText.implicitWidth + 28
        radius: 9
        color: presetTap.pressed ? "#ead9bd" : "#fff7e8"
        border.width: 1
        border.color: "#d9b87d"
        scale: presetTap.pressed ? 0.96 : 1
        Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }
        Row {
            anchors.centerIn: parent
            spacing: 0
            AppText {
                id: presetText
                color: "#6f4b10"
                font.pixelSize: 11
                font.weight: Font.Medium
            }
        }
        TapHandler {
            id: presetTap
            onTapped: preset.clicked()
        }
    }

    component SourceTile: Rectangle {
        id: tile
        property string label
        property url iconSource
        property bool selected: false
        signal clicked
        Layout.fillWidth: true
        Layout.preferredHeight: 68
        radius: 11
        color: selected ? "#f0faf8" : "#ffffff"
        border.width: selected ? 2 : 1
        border.color: selected ? window.accent : "#dfe4e6"
        Column {
            anchors.centerIn: parent
            spacing: 7
            Image {
                anchors.horizontalCenter: parent.horizontalCenter
                source: tile.iconSource
                width: 25
                height: 25
                fillMode: Image.PreserveAspectFit
                smooth: true
            }
            AppText {
                anchors.horizontalCenter: parent.horizontalCenter
                text: tile.label
                color: tile.selected ? window.accentDark : window.textPrimary
                font.pixelSize: 12
                font.weight: Font.Medium
            }
        }
        Rectangle {
            visible: tile.selected
            width: 18
            height: 18
            radius: 9
            color: window.accent
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 6
            AppText {
                anchors.centerIn: parent
                text: "✓"
                color: "white"
                font.pixelSize: 11
                font.weight: Font.Bold
            }
        }
        scale: tileTap.pressed ? 0.97 : 1
        Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }
        TapHandler {
            id: tileTap
            onTapped: tile.clicked()
        }
    }

    component ResizeHandle: MouseArea {
        property int resizeEdges: 0
        enabled: window.visibility !== Window.Maximized
        acceptedButtons: Qt.LeftButton
        onPressed: window.startSystemResize(resizeEdges)
    }

    component BatchToolPage: Item {
        objectName: "batchToolPage"
        property int selectedRow: -1
        property var selectedRows: []
        property int selectionAnchor: -1
        property int importRow: -1
        property var detailItem: null

        function selectMovieRow(row, modifiers) {
            let next = selectedRows.slice()
            if ((modifiers & Qt.ShiftModifier) && selectionAnchor >= 0) {
                next = []
                for (let value = Math.min(selectionAnchor, row);
                     value <= Math.max(selectionAnchor, row); ++value)
                    next.push(value)
            } else if (modifiers & Qt.ControlModifier) {
                const position = next.indexOf(row)
                if (position >= 0)
                    next.splice(position, 1)
                else
                    next.push(row)
                selectionAnchor = row
            } else {
                next = [row]
                selectionAnchor = row
            }
            selectedRows = next
            selectedRow = next.indexOf(row) >= 0 ? row : (next.length ? next[next.length - 1] : -1)
        }

        function selectedMovieItems() {
            const items = backend.batchItems
            return selectedRows.map(row => items[row]).filter(item => item !== undefined)
        }

        function canRemoveSelectedMovies() {
            const items = selectedMovieItems()
            return items.length > 0 && !backend.batchRunning && items.every(item => !item.processing)
        }

        function canProcessSelectedMovies() {
            if (backend.batchRunning)
                return false
            return selectedMovieItems().some(item => !item.burned && !item.processing &&
                                                  item.taskState !== "completed" &&
                                                  (item.state === "ready" || item.state === "review"))
        }

        function canStopSelectedMovies() {
            return selectedMovieItems().some(item => item.stoppable)
        }

        function removeSelectedMovies() {
            if (!selectedRows.length)
                return
            const scrollY = batchList.contentY
            if (!backend.batchRemoveRows(selectedRows))
                return
            selectedRows = []
            selectedRow = -1
            selectionAnchor = -1
            Qt.callLater(function() {
                batchList.contentY = Math.max(0, Math.min(scrollY, batchList.contentHeight - batchList.height))
            })
        }

        NativeDialogs.FileDialog {
            id: addMovieDialog
            objectName: "addMovieDialog"
            title: "添加影片"
            fileMode: NativeDialogs.FileDialog.OpenFiles
            nameFilters: ["视频文件 (*.mkv *.mp4 *.mov *.avi *.m4v)"]
            onAccepted: backend.handleDroppedUrls(selectedFiles, 0)
        }

        NativeDialogs.FolderDialog {
            id: addFolderDialog
            objectName: "addFolderDialog"
            title: "添加影片文件夹（包含所有下级文件夹）"
            onAccepted: backend.handleDroppedUrls([selectedFolder], 0)
        }

        NativeDialogs.FileDialog {
            id: importSubtitleDialog
            objectName: "importSubtitleDialog"
            title: "导入本地字幕"
            fileMode: NativeDialogs.FileDialog.OpenFile
            nameFilters: ["字幕文件 (*.srt *.ass *.ssa *.vtt)", "所有文件 (*)"]
            onAccepted: backend.batchImportSubtitleFile(importRow, selectedFile)
        }

        function showDetails(item) {
            detailItem = item
            batchDetailDialog.open()
        }

        Dialog {
            id: batchDetailDialog
            parent: Overlay.overlay
            anchors.centerIn: parent
            width: Math.min(parent.width - 80, 760)
            height: Math.min(parent.height - 80, 500)
            modal: true
            closePolicy: Popup.CloseOnEscape
            padding: 0
            background: Rectangle {
                radius: 16
                color: "#ffffff"
                border.width: 1
                border.color: "#dce4e5"
            }
            contentItem: ColumnLayout {
                spacing: 0
                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 58
                    Layout.leftMargin: 20
                    Layout.rightMargin: 14
                    AppText {
                        text: "影片详情"
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                    }
                    Item { Layout.fillWidth: true }
                    PillButton { text: "关闭"; onClicked: batchDetailDialog.close() }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: "#e7ecec"
                }
                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 18
                    ScrollBar.vertical.policy: ScrollBar.AsNeeded
                    TextArea {
                        readOnly: true
                        wrapMode: TextEdit.Wrap
                        selectByMouse: true
                        color: window.textPrimary
                        font.family: window.fontFamily
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        text: detailItem ? "影片：" + detailItem.name +
                                           "\n\n路径：" + detailItem.path +
                                           "\n\n音轨：" + detailItem.audioDetail +
                                           "\n现有字幕：" + detailItem.subtitles +
                                           "\n套用偏好：" + detailItem.profile +
                                           "\n\n处理方案：" + detailItem.summary +
                                           (detailItem.detail ? "\n\n详细说明：" + detailItem.detail : "") +
                                           "\n\n状态：" + detailItem.status : ""
                        background: Rectangle {
                            radius: 10
                            color: "#f7f9f9"
                            border.width: 1
                            border.color: "#e1e7e8"
                        }
                    }
                }
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                AppText { text: "影片处理"; font.pixelSize: 21; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                PillButton { objectName: "addMovieButton"; text: "添加影片"; onClicked: addMovieDialog.open() }
                PillButton { objectName: "addFolderButton"; text: "添加文件夹"; onClicked: addFolderDialog.open() }
                PillButton { objectName: "removeSelectedButton"; text: "移除所选"; onClicked: removeSelectedMovies() }
                PillButton { text: "清空"; onClicked: { backend.batchClear(); selectedRows = []; selectedRow = -1; selectionAnchor = -1 } }
                PillButton { text: "分析全部"; selected: true; onClicked: backend.batchAnalyze() }
                PillButton { text: "开始处理"; selected: true; onClicked: backend.batchStart() }
                PillButton { text: "停止"; onClicked: backend.batchStop() }
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 14
                color: "#ffffff"
                border.width: 1
                border.color: "#dce3e4"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 0
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        radius: 9
                        color: "#eef3f3"
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            spacing: 8
                            AppText { text: "影片"; Layout.preferredWidth: 180; font.weight: Font.DemiBold }
                            AppText { text: "主音轨"; Layout.preferredWidth: 95; font.weight: Font.DemiBold }
                            AppText { text: "音频编码"; Layout.preferredWidth: 80; font.weight: Font.DemiBold }
                            AppText { text: "现有字幕"; Layout.preferredWidth: 105; font.weight: Font.DemiBold }
                            AppText { text: "偏好"; Layout.preferredWidth: 72; font.weight: Font.DemiBold }
                            AppText { text: "最终处理方案"; Layout.fillWidth: true; font.weight: Font.DemiBold }
                            AppText { text: "状态"; Layout.preferredWidth: 60; font.weight: Font.DemiBold }
                        }
                    }
                    ListView {
                        id: batchList
                        objectName: "batchList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        rightMargin: batchListBar.width + 4
                        model: backend.batchItems
                        ScrollBar.vertical: ScrollBar {
                            id: batchListBar
                            parent: batchList
                            anchors.top: batchList.top
                            anchors.right: batchList.right
                            anchors.bottom: batchList.bottom
                            policy: ScrollBar.AlwaysOn
                        }
                        delegate: Rectangle {
                            objectName: "movieRow"
                            required property int index
                            required property var modelData
                            width: batchList.width
                            height: 48
                            color: modelData.burned ? "#173f7a" :
                                   modelData.audioWarning ? "#fff3da" :
                                   modelData.state === "ready" ? "#e8f5f1" :
                                   modelData.state === "review" ? "#fff3da" :
                                   modelData.state === "blocked" || modelData.state === "failed" ? "#fde9e7" :
                                   index % 2 ? "#f8faf9" : "#ffffff"
                            Rectangle {
                                anchors.fill: parent
                                visible: selectedRows.indexOf(index) >= 0
                                color: "#2607998d"
                                border.width: 2
                                border.color: window.accent
                                z: 1
                            }
                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12
                                spacing: 8
                                z: 2
                                AppText { text: modelData.name; color: modelData.burned ? "white" : window.textPrimary; Layout.preferredWidth: 180; elide: Text.ElideMiddle }
                                AppText { text: modelData.audio; color: modelData.burned ? "white" : window.textPrimary; Layout.preferredWidth: 95; elide: Text.ElideRight }
                                AppText { text: modelData.codecs; color: modelData.burned ? "white" : window.textPrimary; Layout.preferredWidth: 80; elide: Text.ElideRight }
                                AppText { text: modelData.subtitles; color: modelData.burned ? "white" : window.textPrimary; Layout.preferredWidth: 105; elide: Text.ElideRight }
                                ComboBox {
                                    objectName: "batchPreferenceCombo"
                                    Layout.preferredWidth: 72
                                    height: 30
                                    model: ["偏好 1", "偏好 2", "偏好 3", "偏好 4"]
                                    currentIndex: modelData.profileSlot - 1
                                    font.family: window.fontFamily
                                    font.pixelSize: 10
                                    enabled: modelData.state === "pending" && !backend.batchRunning && !modelData.burned && !modelData.processing
                                    z: 4
                                    onActivated: selectedIndex => backend.batchSetProfile(index, selectedIndex + 1)
                                }
                                AppText {
                                    text: modelData.summary
                                    color: modelData.burned ? "white" : window.textPrimary
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                    clip: true
                                }
                                AppText {
                                    text: modelData.status
                                    color: modelData.burned ? "white" : window.textPrimary
                                    Layout.preferredWidth: 60
                                    maximumLineCount: 1
                                    clip: true
                                }
                            }
                            Menu {
                                id: batchContextMenu
                                objectName: "movieContextMenu"
                                MenuItem {
                                    objectName: "contextPlayVideo"
                                    text: "播放视频"
                                    onTriggered: backend.batchPlayVideo(index)
                                }
                                MenuItem {
                                    objectName: "contextRemoveSelected"
                                    text: "移除选中"
                                    enabled: canRemoveSelectedMovies()
                                    onTriggered: removeSelectedMovies()
                                }
                                MenuSeparator {}
                                MenuItem {
                                    objectName: "contextProcessSelected"
                                    text: "处理选中"
                                    enabled: canProcessSelectedMovies()
                                    onTriggered: backend.batchStartRows(selectedRows)
                                }
                                MenuItem {
                                    objectName: "contextStopSelected"
                                    text: "停止选中"
                                    enabled: canStopSelectedMovies()
                                    onTriggered: backend.batchStopRows(selectedRows)
                                }
                                MenuSeparator {}
                                MenuItem {
                                    text: "智能字幕"
                                    enabled: !modelData.burned && !modelData.processing
                                    onTriggered: backend.batchSmartSubtitles(index)
                                }
                                MenuItem {
                                    text: "在线手动字幕"
                                    enabled: !modelData.burned && !modelData.processing
                                    onTriggered: backend.batchManualSubtitles(index)
                                }
                                MenuItem {
                                    text: "导入本地字幕"
                                    enabled: !modelData.burned && !modelData.processing
                                    onTriggered: {
                                        importRow = index
                                        importSubtitleDialog.currentFolder = backend.batchMovieFolderUrl(index)
                                        importSubtitleDialog.open()
                                    }
                                }
                                MenuSeparator {}
                                MenuItem {
                                    objectName: "contextShowDetails"
                                    text: "查看详情"
                                    onTriggered: showDetails(modelData)
                                }
                            }
                            MouseArea {
                                anchors.fill: parent
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                hoverEnabled: false
                                z: 1
                                onClicked: mouse => {
                                    if (mouse.button === Qt.RightButton) {
                                        batchContextMenu.popup()
                                    } else {
                                        selectMovieRow(index, mouse.modifiers)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                radius: 15
                color: "#e7eeee"
                Rectangle {
                    width: parent.width * backend.batchProgress / 100
                    height: parent.height
                    radius: parent.radius
                    color: window.accent
                }
                AppText { anchors.centerIn: parent; text: backend.batchStatus; font.pixelSize: 12 }
            }
            ScrollView {
                id: batchLogScroll
                objectName: "batchLogScroll"
                Layout.fillWidth: true
                Layout.preferredHeight: 120
                clip: true
                rightPadding: batchLogBar.width + 4
                property bool restoringScroll: false
                property bool initializedScroll: false
                readonly property real maximumScrollPosition: Math.max(0, contentItem.contentHeight - contentItem.height)

                function saveScrollState() {
                    if (restoringScroll)
                        return
                    window.batchLogSavedY = contentItem.contentY
                    window.batchLogFollowLatest = maximumScrollPosition - contentItem.contentY <= 8
                }

                function replaceLogText() {
                    let position = initializedScroll ? contentItem.contentY : window.batchLogSavedY
                    let followLatest = initializedScroll
                            ? maximumScrollPosition - position <= 8
                            : window.batchLogFollowLatest
                    restoringScroll = true
                    batchLogArea.text = backend.batchLogText
                    Qt.callLater(function() {
                        let maximum = Math.max(0, batchLogScroll.contentItem.contentHeight - batchLogScroll.contentItem.height)
                        batchLogScroll.contentItem.contentY = followLatest ? maximum : Math.max(0, Math.min(position, maximum))
                        batchLogScroll.restoringScroll = false
                        batchLogScroll.initializedScroll = true
                        batchLogScroll.saveScrollState()
                    })
                }

                Component.onDestruction: saveScrollState()

                ScrollBar.vertical: ScrollBar {
                    id: batchLogBar
                    parent: batchLogScroll
                    anchors.top: batchLogScroll.top
                    anchors.right: batchLogScroll.right
                    anchors.bottom: batchLogScroll.bottom
                    policy: ScrollBar.AlwaysOn
                }

                TextArea {
                    id: batchLogArea
                    objectName: "batchLogArea"
                    readOnly: true
                    text: ""
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    color: window.textPrimary
                    font.family: window.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    background: Rectangle { color: "#f8fafb"; radius: 12; border.width: 1; border.color: "#e1e7e8" }
                    Component.onCompleted: batchLogScroll.replaceLogText()
                }

                Connections {
                    target: backend
                    function onToolStateChanged() {
                        batchLogScroll.replaceLogText()
                    }
                }

                Connections {
                    target: batchLogScroll.contentItem
                    function onContentYChanged() {
                        batchLogScroll.saveScrollState()
                    }
                }
            }
        }
    }

    component ProfileToolPage: Item {
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 14
            RowLayout {
                Layout.fillWidth: true
                AppText { text: "偏好设置"; font.pixelSize: 21; font.weight: Font.DemiBold }
                AppText { text: "选择并编辑四套批量处理偏好"; color: window.textSecondary; font.pixelSize: 13; Layout.leftMargin: 12 }
                Item { Layout.fillWidth: true }
            }
            AppText {
                visible: backend.profileEditingLocked
                text: "当前批次已经开始分析，偏好已锁定；请先清空批次，再修改偏好并重新分析。"
                color: "#b45309"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                enabled: !backend.profileEditingLocked
                opacity: enabled ? 1.0 : 0.55
                Rectangle {
                    Layout.preferredHeight: 42
                    Layout.preferredWidth: editPreferenceRow.implicitWidth + 22
                    radius: 11
                    color: "#f4f7f7"
                    border.width: 1
                    border.color: "#dce4e5"
                    Row {
                        id: editPreferenceRow
                        anchors.centerIn: parent
                        spacing: 7
                        AppText { text: "编辑偏好"; color: window.textSecondary; font.pixelSize: 11 }
                        Repeater {
                            model: 4
                            delegate: PillButton {
                                required property int index
                                text: "偏好 " + (index + 1)
                                selected: backend.profileSlot === index + 1
                                onClicked: backend.profileSelect(index + 1)
                            }
                        }
                    }
                }
                Item { Layout.fillWidth: true }
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: !backend.profileEditingLocked
                opacity: enabled ? 1.0 : 0.55
                AppText { text: "偏好名称"; font.weight: Font.DemiBold }
                TextField {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    text: backend.profileName
                    font.family: window.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    selectByMouse: true
                    background: Rectangle {
                        radius: 8
                        color: "#ffffff"
                        border.width: parent.activeFocus ? 2 : 1
                        border.color: parent.activeFocus ? window.accent : "#d7dfe1"
                    }
                    onTextEdited: backend.profileSetName(text)
                    onEditingFinished: backend.profileSetName(text)
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 16
                enabled: !backend.profileEditingLocked
                opacity: enabled ? 1.0 : 0.55
                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 15
                    color: "#ffffff"
                    border.width: 1
                    border.color: "#dfe5e6"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        AppText { text: "音频偏好"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Repeater {
                                model: [
                                    { code: "native", title: "原生音频", description: "不修改影片原有音轨，不删除、不转码、不新增。" },
                                    { code: "universal", title: "通用兼容", description: "保留高品质原音轨，并确保成品包含一条通用音轨。" },
                                    { code: "compact", title: "精简兼容", description: "成品只保留一条通用音轨，减少音轨数量和文件体积。" }
                                ]
                                delegate: Rectangle {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 58
                                    radius: 11
                                    color: backend.profileAudioPolicy === modelData.code ? "#eaf8f5" : "#ffffff"
                                    border.width: backend.profileAudioPolicy === modelData.code ? 2 : 1
                                    border.color: backend.profileAudioPolicy === modelData.code ? window.accent : "#dce3e4"
                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 10
                                        RadioButton {
                                            checked: backend.profileAudioPolicy === modelData.code
                                            onClicked: backend.profileSetAudioPolicy(modelData.code)
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 1
                                            RowLayout {
                                                AppText { text: modelData.title; font.pixelSize: 13; font.weight: Font.DemiBold }
                                            }
                                            AppText { text: modelData.description; color: window.textSecondary; font.pixelSize: 11 }
                                        }
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        onClicked: backend.profileSetAudioPolicy(modelData.code)
                                    }
                                }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: compactWarning.implicitHeight + 20
                            visible: backend.profileAudioPolicy === "compact"
                            radius: 9
                            color: "#fff3d6"
                            border.width: 1
                            border.color: "#e5c06b"
                            AppText {
                                id: compactWarning
                                anchors.fill: parent
                                anchors.margins: 10
                                wrapMode: Text.WordWrap
                                color: "#8a5a00"
                                font.pixelSize: 11
                                text: "该模式最终仅保留一条通用音轨，TrueHD Atmos、DTS-HD、DTS:X 等高品质音轨将在输出验证成功后删除。"
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 15
                    color: "#ffffff"
                    border.width: 1
                    border.color: "#dfe5e6"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        AppText { text: "最终需要的字幕"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        AppText { text: "按语言选择成品字幕；下载字幕可按下方选项替换同语言原轨。"; color: window.textSecondary; font.pixelSize: 12 }
                        GridLayout {
                            Layout.fillWidth: true
                            Layout.topMargin: 4
                            columns: 4
                            columnSpacing: 10
                            rowSpacing: 2
                            Repeater {
                                model: backend.profileSubtitleOptions
                                delegate: CheckBox {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    text: modelData.label
                                    checked: modelData.checked
                                    font.family: window.fontFamily
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    onToggled: backend.profileToggleSubtitle(modelData.code, checked)
                                }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            Layout.topMargin: 6
                            Layout.bottomMargin: 4
                            color: "#e5eaea"
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            CheckBox {
                                text: "保留下载字幕并替换"
                                checked: backend.profileReplaceDownloadedSubtitle
                                font.family: window.fontFamily
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                onToggled: backend.profileSetFlag("replaceDownloaded", checked)
                            }
                            AppText {
                                Layout.fillWidth: true
                                Layout.leftMargin: 28
                                text: "勾选后，已核验的下载字幕写入成品，并替换同语言内嵌字幕。"
                                color: window.textSecondary
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.topMargin: 4
                            spacing: 0
                            CheckBox {
                                text: "简繁同源"
                                checked: backend.profileChineseScriptEquivalent
                                font.family: window.fontFamily
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                onToggled: backend.profileSetFlag("chineseScriptEquivalent", checked)
                            }
                            AppText {
                                Layout.fillWidth: true
                                Layout.leftMargin: 28
                                text: "只有一种完整中文字幕时直接保留，不进行简繁转换。"
                                color: window.textSecondary
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
            AppText {
                visible: backend.profileFeedback.length > 0
                text: backend.profileFeedback
                color: window.accentDark
                font.pixelSize: 13
            }
        }
    }

    component HardwareToolPage: Item {
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 16
            RowLayout {
                Layout.fillWidth: true
                AppText { text: "硬件状态"; font.pixelSize: 21; font.weight: Font.DemiBold }
                AppText { text: "检测结果决定批量并行数与推荐模式"; color: window.textSecondary; font.pixelSize: 13; Layout.leftMargin: 12 }
                Item { Layout.fillWidth: true }
                PillButton { text: "重新检测"; selected: true; onClicked: backend.hardwareRefresh() }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 16
                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 15
                    color: "#ffffff"
                    border.width: 1
                    border.color: "#dfe5e6"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 20
                        AppText { text: "软件配置建议"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        AppText { Layout.fillWidth: true; Layout.fillHeight: true; text: backend.hardwareRequirements; wrapMode: Text.Wrap; verticalAlignment: Text.AlignTop; font.pixelSize: 13; lineHeight: 1.45 }
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 15
                    color: "#ffffff"
                    border.width: 1
                    border.color: "#dfe5e6"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 20
                        AppText { text: "本机检测结果"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        AppText { Layout.fillWidth: true; text: backend.hardwareStatus; wrapMode: Text.Wrap; font.pixelSize: 16; font.weight: Font.DemiBold }
                        AppText { Layout.fillWidth: true; text: backend.hardwareDetail; wrapMode: Text.Wrap; font.pixelSize: 13; lineHeight: 1.4 }
                        AppText { Layout.fillWidth: true; text: backend.hardwareReason; wrapMode: Text.Wrap; color: window.textSecondary; font.pixelSize: 13; lineHeight: 1.4 }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }

    component SubtitleServicePage: Item {
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 16
            RowLayout {
                Layout.fillWidth: true
                AppText { text: "字幕服务"; font.pixelSize: 21; font.weight: Font.DemiBold }
                AppText {
                    text: "API Key 只需配置一次，影片需要字幕时会自动使用"
                    color: window.textSecondary
                    font.pixelSize: 13
                    Layout.leftMargin: 12
                }
                Item { Layout.fillWidth: true }
            }
            Repeater {
                model: [
                    { code: "opensubtitles", name: "OpenSubtitles", key: backend.openSubtitlesKey, status: backend.openSubtitlesStatus },
                    { code: "subdl", name: "SubDL", key: backend.subdlKey, status: backend.subdlStatus }
                ]
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredHeight: 92
                    radius: 14
                    color: "#ffffff"
                    border.width: 1
                    border.color: "#dfe5e6"
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 16
                        spacing: 10
                        AppText {
                            text: modelData.name
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            Layout.preferredWidth: 130
                        }
                        TextField {
                            id: keyField
                            Layout.fillWidth: true
                            text: modelData.key
                            echoMode: TextInput.Password
                            placeholderText: modelData.name + " API Key"
                            font.family: window.fontFamily
                            selectByMouse: true
                        }
                        AppText {
                            text: modelData.status
                            color: modelData.status.indexOf("失败") >= 0 ? "#b8493f" : window.textSecondary
                            font.pixelSize: 12
                            Layout.preferredWidth: 180
                            elide: Text.ElideRight
                        }
                        PillButton {
                            text: "获取 Key"
                            onClicked: backend.openSubtitleKeyPage(modelData.code)
                        }
                        PillButton {
                            text: "验证并保存"
                            enabled: modelData.status !== "正在验证…"
                            selected: true
                            onClicked: backend.saveSubtitleService(modelData.code, keyField.text)
                        }
                    }
                }
            }
            Item { Layout.fillHeight: true }
        }
    }

    Rectangle {
        id: surface
        anchors.fill: parent
        radius: 16
        color: window.canvasColor
        border.width: 1
        border.color: "#dfe5e6"
        clip: true

        ColumnLayout {
            width: Math.min(parent.width - 2, 1360)
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.margins: 1
            spacing: 0

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 42
                color: "transparent"
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 16
                    anchors.rightMargin: 10
                    spacing: 8
                    Rectangle {
                        width: 28
                        height: 28
                        radius: 9
                        gradient: Gradient {
                            GradientStop { position: 0; color: "#12b7aa" }
                            GradientStop { position: 1; color: "#047f77" }
                        }
                        AppText {
                            anchors.centerIn: parent
                            text: "▶"
                            color: "white"
                            font.pixelSize: 14
                        }
                    }
                    AppText {
                        text: "SubFlow"
                        font.pixelSize: 20
                        font.weight: Font.Medium
                    }
                    AppText {
                        text: "v" + appVersion
                        color: "#4f585c"
                        font.pixelSize: 11
                        Layout.alignment: Qt.AlignBottom
                        Layout.bottomMargin: 5
                    }
                    Item { Layout.fillWidth: true }
                    Repeater {
                        model: [
                            { label: "—", action: "min" },
                            { label: "□", action: "max" },
                            { label: "×", action: "close" }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            width: 34
                            height: 28
                            radius: 9
                            color: controlHover.hovered ? (modelData.action === "close" ? "#e95a52" : "#e9eeee") : "transparent"
                            AppText {
                                anchors.centerIn: parent
                                text: modelData.label
                                color: controlHover.hovered && modelData.action === "close" ? "white" : "#313a3e"
                                font.pixelSize: 18
                            }
                            HoverHandler { id: controlHover }
                            TapHandler {
                                onTapped: {
                                    if (modelData.action === "min") window.showMinimized()
                                    else if (modelData.action === "max") window.toggleMaximized()
                                    else window.close()
                                }
                            }
                        }
                    }
                }
                MouseArea {
                    property point pressPosition
                    anchors.fill: parent
                    anchors.rightMargin: 126
                    acceptedButtons: Qt.LeftButton
                    onPressed: mouse => pressPosition = Qt.point(mouse.x, mouse.y)
                    onPositionChanged: mouse => {
                        if (!pressed || window.visibility === Window.Maximized)
                            return
                        window.x += mouse.x - pressPosition.x
                        window.y += mouse.y - pressPosition.y
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                color: "transparent"
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 18
                    anchors.rightMargin: 18
                    spacing: 10
                    Repeater {
                        model: [
                            { text: "影片处理", icon: "../assets/batch.svg" },
                            { text: "偏好", icon: "../assets/preferences.svg" },
                            { text: "字幕服务", icon: "../assets/online.svg" },
                            { text: "硬件", icon: "../assets/hardware.svg" }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            required property int index
                            width: navRow.implicitWidth + 22
                            height: 32
                            radius: 16
                            color: index === window.currentPage ? "#ffffff" : "transparent"
                            scale: navTap.pressed ? 0.96 : 1
                            Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }
                            Row {
                                id: navRow
                                anchors.centerIn: parent
                                spacing: 6
                                Image { source: modelData.icon; width: 16; height: 16 }
                                AppText {
                                    text: modelData.text
                                    color: index === window.currentPage ? window.accentDark : "#4e585d"
                                    font.pixelSize: 12
                                    font.weight: Font.Medium
                                }
                            }
                            TapHandler {
                                id: navTap
                                onTapped: {
                                    window.currentPage = index
                                    backend.activateToolPage(index)
                                }
                            }
                        }
                    }
                    Item { Layout.fillWidth: true }
                    Rectangle {
                        width: 150
                        height: 32
                        radius: 10
                        color: "#eef7f5"
                        border.width: 1
                        border.color: "#d0e4df"
                        AppText {
                            anchors.centerIn: parent
                            text: backend.performanceSummary
                            color: "#315954"
                            font.pixelSize: 10
                        }
                    }
                }
            }

            SoftCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 12
                Layout.rightMargin: 12
                Layout.bottomMargin: 10
                Loader {
                    anchors.fill: parent
                    sourceComponent: window.currentPage === 0 ? batchToolPageComponent :
                                     window.currentPage === 1 ? profileToolPageComponent :
                                     window.currentPage === 2 ? subtitleServicePageComponent :
                                     hardwareToolPageComponent
                }
                Component { id: batchToolPageComponent; BatchToolPage {} }
                Component { id: profileToolPageComponent; ProfileToolPage {} }
                Component { id: hardwareToolPageComponent; HardwareToolPage {} }
                Component { id: subtitleServicePageComponent; SubtitleServicePage {} }
            }
        }
    }

    DropArea {
        id: fileDropArea
        anchors.fill: parent
        z: 70
        enabled: licenseBridge.valid
        onDropped: drop => {
            var destinationPage = backend.handleDroppedUrls(drop.urls, window.currentPage)
            if (destinationPage !== window.currentPage) {
                window.currentPage = destinationPage
                if (destinationPage > 0)
                    backend.activateToolPage(destinationPage)
            }
            drop.acceptProposedAction()
        }
        Rectangle {
            anchors.fill: parent
            anchors.margins: 8
            visible: fileDropArea.containsDrag
            radius: 14
            color: "#d9f4f0"
            opacity: 0.92
            border.width: 2
            border.color: window.accent
            AppText {
                anchors.centerIn: parent
                text: "松开以添加影片或文件夹"
                color: window.accentDark
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
        }
    }


    Rectangle {
        parent: surface
        anchors.fill: parent
        z: 100
        visible: !licenseBridge.valid
        color: "#990f1b1d"

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            preventStealing: true
            propagateComposedEvents: false
        }

        SoftCard {
            width: 520
            height: 370
            anchors.centerIn: parent

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 34
                spacing: 16

                Rectangle {
                    Layout.alignment: Qt.AlignHCenter
                    width: 52
                    height: 52
                    radius: 18
                    gradient: Gradient {
                        GradientStop { position: 0; color: "#12b7aa" }
                        GradientStop { position: 1; color: "#047f77" }
                    }
                    AppText {
                        anchors.centerIn: parent
                        text: "▶"
                        color: "white"
                        font.pixelSize: 18
                    }
                }
                AppText {
                    Layout.alignment: Qt.AlignHCenter
                    text: "激活 SubFlow"
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                }
                AppText {
                    Layout.alignment: Qt.AlignHCenter
                    text: "首次使用需要绑定当前电脑"
                    color: window.textSecondary
                    font.pixelSize: 13
                }
                TextField {
                    id: activationKey
                    Layout.fillWidth: true
                    Layout.preferredHeight: 48
                    placeholderText: "请输入激活码"
                    enabled: !licenseBridge.busy
                    font.family: window.fontFamily
                    font.pixelSize: 14
                    horizontalAlignment: TextInput.AlignHCenter
                    verticalAlignment: TextInput.AlignVCenter
                    leftPadding: 16
                    rightPadding: 16
                    topPadding: 0
                    bottomPadding: 0
                    background: Rectangle {
                        radius: 12
                        color: "#f7f9fa"
                        border.width: activationKey.activeFocus ? 2 : 1
                        border.color: activationKey.activeFocus ? window.accent : "#d9e0e2"
                    }
                    onAccepted: licenseBridge.activate(text)
                }
                AppText {
                    Layout.alignment: Qt.AlignHCenter
                    text: "设备码：" + licenseBridge.deviceCode
                    color: "#7a8589"
                    font.pixelSize: 11
                }
                AppText {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: licenseBridge.message
                    color: licenseBridge.busy ? window.textSecondary : "#b54740"
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 50
                    radius: 25
                    color: licenseBridge.busy ? "#8abdb7" : window.accent
                    Row {
                        anchors.centerIn: parent
                        spacing: 10
                        BusyIndicator {
                            visible: licenseBridge.busy
                            running: visible
                            width: 22
                            height: 22
                        }
                        AppText {
                            text: licenseBridge.busy ? "正在验证…" : "激活并进入"
                            color: "white"
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        enabled: !licenseBridge.busy
                        preventStealing: true
                        propagateComposedEvents: false
                        onClicked: licenseBridge.activate(activationKey.text)
                    }
                }
            }
        }
    }

    ResizeHandle {
        z: 300
        resizeEdges: Qt.LeftEdge
        cursorShape: Qt.SizeHorCursor
        width: 6
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 10
        anchors.bottomMargin: 10
    }
    ResizeHandle {
        z: 300
        resizeEdges: Qt.RightEdge
        cursorShape: Qt.SizeHorCursor
        width: 6
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 10
        anchors.bottomMargin: 10
    }
    ResizeHandle {
        z: 300
        resizeEdges: Qt.TopEdge
        cursorShape: Qt.SizeVerCursor
        height: 6
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 10
        anchors.rightMargin: 10
    }
    ResizeHandle {
        z: 300
        resizeEdges: Qt.BottomEdge
        cursorShape: Qt.SizeVerCursor
        height: 6
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 10
        anchors.rightMargin: 10
    }
    ResizeHandle {
        z: 301
        resizeEdges: Qt.TopEdge | Qt.LeftEdge
        cursorShape: Qt.SizeFDiagCursor
        width: 12
        height: 12
        anchors.left: parent.left
        anchors.top: parent.top
    }
    ResizeHandle {
        z: 301
        resizeEdges: Qt.TopEdge | Qt.RightEdge
        cursorShape: Qt.SizeBDiagCursor
        width: 12
        height: 12
        anchors.right: parent.right
        anchors.top: parent.top
    }
    ResizeHandle {
        z: 301
        resizeEdges: Qt.BottomEdge | Qt.LeftEdge
        cursorShape: Qt.SizeBDiagCursor
        width: 12
        height: 12
        anchors.left: parent.left
        anchors.bottom: parent.bottom
    }
    ResizeHandle {
        z: 301
        resizeEdges: Qt.BottomEdge | Qt.RightEdge
        cursorShape: Qt.SizeFDiagCursor
        width: 12
        height: 12
        anchors.right: parent.right
        anchors.bottom: parent.bottom
    }
}
