import mysql from 'mysql2/promise';

export const handler = async () => {
  const connection = await mysql.createConnection({
    host: process.env.DB_HOST,
    user: 'nodeapp',
    password: 'coffee',
    database: 'COFFEE',
    connectTimeout: 20000
  });
  try {
    const [suppliers] = await connection.query('SELECT * FROM suppliers ORDER BY id');
    const [beans] = await connection.query('SELECT * FROM beans ORDER BY supplier_id, id');
    const my_json_arr = suppliers.map(supplier => ({
      suppliers_id_int: supplier.id,
      supplier_name_str: supplier.name,
      supplier_address_str: supplier.address,
      supplier_phone_str: supplier.phone,
      bean_info_obj_arr: beans.filter(bean => bean.supplier_id === supplier.id).map(bean => ({
        type_str: bean.type,
        product_name_str: bean.product_name,
        quantity_int: bean.quantity
      }))
    }));
    return { statusCode: 200, body: JSON.stringify({ my_json_arr }) };
  } finally {
    await connection.end();
  }
};
