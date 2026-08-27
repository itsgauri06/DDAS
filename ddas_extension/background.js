console.log("DDAS background service worker started.");

const suspiciousExtensions = [
    ".exe",
    ".msi",
    ".scr",
    ".bat",
    ".cmd",
    ".com",
    ".ps1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".hta",
    ".dll"
];

const documentExtensions = [
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".jpg",
    ".jpeg",
    ".png",
    ".txt",
    ".zip"
];

async function sendToDashboard(data) {
    try {
        await fetch("http://127.0.0.1:5000/event", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                source: "extension",
                event: "suspicious_download",
                data: data
            })
        });

        console.log("Sent to DDAS dashboard");
    } catch (error) {
        console.error("Could not connect to DDAS bridge:", error);
    }
}

const pendingWarnings = new Map();


function getExtension(filename) {
    if (!filename) {
        return "";
    }

    const lastDot = filename.lastIndexOf(".");

    if (lastDot === -1) {
        return "";
    }

    return filename.substring(lastDot).toLowerCase();
}


function hasDoubleExtension(filename) {
    if (!filename) {
        return false;
    }

    const lower = filename.toLowerCase();

    for (const documentExtension of documentExtensions) {
        for (const suspiciousExtension of suspiciousExtensions) {

            if (
                lower.endsWith(
                    documentExtension + suspiciousExtension
                )
            ) {
                return true;
            }
        }
    }

    return false;
}


function analyzeDownload(download) {

    const url = download.url || "";

    let filename = download.filename || "";

    if (!filename && url) {
        try {
            filename = decodeURIComponent(
                new URL(url).pathname.split("/").pop()
            );
        } catch (error) {
            filename = url;
        }
    }

    const source = filename || url;

    const extensionMatch = source.match(/\.[a-zA-Z0-9]+(?:\?.*)?$/);

    const extension = extensionMatch
        ? extensionMatch[0].split("?")[0].toLowerCase()
        : "";

    console.log(
        "DDAS FILE CHECK:",
        {
            filename: filename,
            url: url,
            extension: extension
        }
    );

    const reasons = [];

    if (hasDoubleExtension(source)) {
        reasons.push(
            "The filename uses a deceptive double extension."
        );
    }

    if (suspiciousExtensions.includes(extension)) {
        reasons.push(
            `The file type (${extension}) can execute code.`
        );
    }

    return {
        suspicious: reasons.length > 0,
        reasons: reasons
    };
}

chrome.downloads.onCreated.addListener(async (download) => {

    const analysisResult = analyzeDownload(download);

    if (!analysisResult.suspicious) {
        return;
    }

    // Pause FIRST, before anything else (dashboard call, storage, notifications).
    // This minimizes the race window where a small file finishes downloading
    // before we get a chance to intervene.
    try {
        await chrome.downloads.pause(download.id);
    } catch (error) {
        console.log("DDAS: pause failed (download may have already finished):", error);
    }

    // When first flagging (inside onCreated, right after sendToDashboard(...) call):
    sendToDashboard({
        downloadId: download.id,
        filename: download.filename || download.url,
        url: download.url,
        reasons: analysisResult.reasons,
        status: "PENDING"
    });

    pendingWarnings.set(download.id, {
        filename: download.filename,
        url: download.url,
        reasons: analysisResult.reasons
    });

    await chrome.storage.local.set({
        [`download_${download.id}`]: {
            filename: download.filename,
            url: download.url,
            reasons: analysisResult.reasons,
            status: "WAITING"
        }
    });

    chrome.notifications.create(
        `ddas_${download.id}`,
        {
            type: "basic",
            title: "⚠️ DDAS Security Warning",
            message: `${download.filename}\n\n` + analysisResult.reasons.join("\n"),
            iconUrl: "icon.png",
            buttons: [
                { title: "Continue Download" },
                { title: "Cancel Download" }
            ],
            priority: 2
        }
    );
});

chrome.notifications.onButtonClicked.addListener(
    async (notificationId, buttonIndex) => {

        if (!notificationId.startsWith("ddas_")) {
            return;
        }

        const downloadId = Number(
            notificationId.replace("ddas_", "")
        );

        if (buttonIndex === 0) {
            await continueDownload(downloadId);
        }

        if (buttonIndex === 1) {
            await cancelDownload(downloadId);
        }

        chrome.notifications.clear(
            notificationId
        );
    }
);


async function continueDownload(downloadId) {
    console.log("DDAS: CONTINUE BUTTON CLICKED:", downloadId);

    const warning = pendingWarnings.get(downloadId);
    if (!warning) {
        console.error("DDAS: No warning found for download:", downloadId);
        return;
    }

    // Try to resume — but don't let a failure here block reporting.
    // If the download already finished (fast/small file) or was never
    // actually paused in time, resume() can throw even though the
    // user's decision to continue is still valid and needs to reach DDAS.
    try {
        await chrome.downloads.resume(downloadId);
        console.log("DDAS: Download resumed");
    } catch (error) {
        console.log("DDAS: resume() failed (likely already completed):", error);
    }

    try {
        await chrome.storage.local.set({
            [`download_${downloadId}`]: {
                filename: warning.filename,
                url: warning.url,
                reasons: warning.reasons,
                status: "CONTINUED"
            }
        });

        await sendToDashboard({
            filename: warning.filename,
            url: warning.url,
            reasons: warning.reasons,
            status: "CONTINUED"
        });

        console.log("DDAS: Security event sent");
    } catch (error) {
        console.error("DDAS CONTINUE ERROR (reporting):", error);
    } finally {
        pendingWarnings.delete(downloadId);
    }
}


async function cancelDownload(downloadId) {
    try {
        const warning = pendingWarnings.get(downloadId);

        try { await chrome.downloads.cancel(downloadId); } catch (e) { }
        try { await chrome.downloads.removeFile(downloadId); } catch (e) { }
        await chrome.downloads.erase({ id: downloadId });

        await chrome.storage.local.set({
            [`download_${downloadId}`]: { status: "CANCELLED" }
        });

        // NEW: tell the dashboard/duplicate-detector this file should be ignored
        if (warning) {
            await sendToDashboard({
                filename: warning.filename,
                url: warning.url,
                reasons: warning.reasons,
                status: "CANCELLED"
            });
        }

        pendingWarnings.delete(downloadId);
    } catch (error) {
        console.error("DDAS could not cancel download:", error);
    }
}
