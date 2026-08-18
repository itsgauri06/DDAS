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

    console.log(
        "DDAS DOWNLOAD EVENT:",
        download.filename,
        download.url
    );

    const analysisResult = analyzeDownload(download);

    console.log(
        "DDAS ANALYSIS:",
        analysisResult
    );

    if (!analysisResult.suspicious) {

        console.log(
            "DDAS: Download considered normal:",
            download.filename
        );

        return;
    }

    console.log(
        "DDAS suspicious download detected:",
        download.filename
    );

    // Send event to dashboard
    sendToDashboard({
        filename: download.filename || download.url,
        url: download.url,
        reasons: analysisResult.reasons
    });

    try {

        // Pause suspicious download
        await chrome.downloads.pause(
            download.id
        );

        console.log(
            "DDAS: Download paused:",
            download.filename
        );

        pendingWarnings.set(
            download.id,
            {
                filename: download.filename,
                url: download.url,
                reasons: analysisResult.reasons
            }
        );

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
                message:
                    `${download.filename}\n\n` +
                    analysisResult.reasons.join("\n"),
                iconUrl: "icon.png",
                buttons: [
                    {
                        title: "Continue Download"
                    },
                    {
                        title: "Cancel Download"
                    }
                ],
                priority: 2
            }
        );

    } catch (error) {

        console.error(
            "DDAS could not pause download:",
            error
        );
    }
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

    console.log(
        "DDAS: CONTINUE BUTTON CLICKED:",
        downloadId
    );

    try {

        const warning = pendingWarnings.get(
            downloadId
        );

        console.log(
            "DDAS: WARNING DATA:",
            warning
        );

        if (!warning) {

            console.error(
                "DDAS: No warning found for download:",
                downloadId
            );

            return;
        }

        console.log(
            "DDAS: Resuming download..."
        );

        await chrome.downloads.resume(
            downloadId
        );

        console.log(
            "DDAS: Download resumed"
        );

        await chrome.storage.local.set({
            [`download_${downloadId}`]: {
                filename: warning.filename,
                url: warning.url,
                reasons: warning.reasons,
                status: "CONTINUED"
            }
        });

        console.log(
            "DDAS: Sending security event to dashboard..."
        );

        await sendToDashboard({
            filename: warning.filename,
            url: warning.url,
            reasons: warning.reasons,
            status: "CONTINUED"
        });

        console.log(
            "DDAS: Security event sent"
        );

        pendingWarnings.delete(
            downloadId
        );

    } catch (error) {

        console.error(
            "DDAS CONTINUE ERROR:",
            error
        );
    }
}


async function cancelDownload(downloadId) {

    try {

        console.log(
            "DDAS: Cancelling download:",
            downloadId
        );

        await chrome.downloads.cancel(
            downloadId
        );

        console.log(
            "DDAS: Download cancelled:",
            downloadId
        );

        await chrome.storage.local.set({
            [`download_${downloadId}`]: {
                status: "CANCELLED"
            }
        });

        pendingWarnings.delete(
            downloadId
        );

    } catch (error) {

        console.error(
            "DDAS could not cancel download:",
            error
        );
    }
}