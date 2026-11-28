import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

const accountId = process.env.R2_ACCOUNT_ID;
const accessKeyId = process.env.R2_ACCESS_KEY_ID;
const secretAccessKey = process.env.R2_SECRET_ACCESS_KEY;
const bucketName = process.env.R2_BUCKET_NAME || "safewalk-images";
const publicBaseUrl = (process.env.R2_PUBLIC_BASE_URL || "").replace(/\/+$/, "");

const s3 = new S3Client({
  region: "auto",
  endpoint: `https://${accountId}.r2.cloudflarestorage.com`,
  credentials: {
    accessKeyId,
    secretAccessKey,
  },
});

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  try {
    if (!accountId || !accessKeyId || !secretAccessKey || !publicBaseUrl) {
      return res.status(500).json({ error: "Missing R2 environment variables" });
    }

    const { fileName, contentType } = req.body || {};
    const safeType = contentType || "image/jpeg";
    const ext = (fileName && fileName.includes("."))
      ? fileName.split(".").pop().toLowerCase().replace(/[^a-z0-9]/g, "")
      : "jpg";
    const key = `hazards/${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${ext || "jpg"}`;

    const command = new PutObjectCommand({
      Bucket: bucketName,
      Key: key,
      ContentType: safeType,
    });

    const uploadUrl = await getSignedUrl(s3, command, { expiresIn: 120 });
    const imageUrl = `${publicBaseUrl}/${key}`;

    return res.status(200).json({ uploadUrl, imageUrl, key });
  } catch (err) {
    console.error("[upload-url error]:", err);
    return res.status(500).json({ error: err.message || "Failed to generate upload URL" });
  }
}
