import { S3Client, PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';

const s3 = new S3Client({});
const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[character]);

export const handler = async event => {
  let data = typeof event === 'string' ? JSON.parse(event) : event;
  if (data.body) data = typeof data.body === 'string' ? JSON.parse(data.body) : data.body;
  if (typeof data.my_json_arr === 'string') data.my_json_arr = JSON.parse(data.my_json_arr);
  if (!Array.isArray(data.my_json_arr)) throw new TypeError('Expected my_json_arr to be an array');
  const suppliers = data.my_json_arr.map(supplier => `
    <section data-role="supplier_info">
      <h2>${escape(supplier.supplier_name_str)}</h2>
      <p>${escape(supplier.supplier_address_str)} : ${escape(supplier.supplier_phone_str)}</p>
      ${supplier.bean_info_obj_arr.map(bean => `<div data-role="bean_info"><h3>${escape(bean.type_str)}</h3><h4>${escape(bean.quantity_int)}</h4><span>${escape(bean.product_name_str)}</span></div>`).join('')}
    </section>`).join('');
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>Bean quantity report</title>
    <style>body{margin:0;background:whitesmoke;font-family:Arial,sans-serif;color:#434343}header{background:#434343;color:#eee;padding:24px;text-align:center}section[data-role=supplier_info]{width:90%;margin:24px auto;border-bottom:2px dotted #505951;padding-bottom:16px}p{font-style:italic}div[data-role=bean_info]{display:inline-block;border:1px solid #434343;border-radius:6px;margin:0 12px 12px 0;padding:12px 24px}h3,h4{margin:12px 0}span{display:block}</style>
    </head><body><main class="report"><header><h1>Report</h1><time>${escape(new Date().toISOString())}</time></header>${suppliers}</main></body></html>`;
  const Bucket = process.env.REPORT_BUCKET;
  const Key = 'report.html';
  await s3.send(new PutObjectCommand({ Bucket, Key, Body: html, ContentType: 'text/html', CacheControl: 'max-age=0' }));
  const presigned_url_str = await getSignedUrl(s3, new GetObjectCommand({ Bucket, Key }), {
    expiresIn: Number(process.env.REPORT_EXPIRY || 120)
  });
  return { msg_str: 'Report published to S3', presigned_url_str };
};
