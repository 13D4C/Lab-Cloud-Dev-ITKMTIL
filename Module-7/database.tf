locals {
  products = {
    for product in jsondecode(file("${path.module}/data/all_products.json")).product_item_arr :
    product.product_name_str => product
  }
}

resource "aws_dynamodb_table" "food_products" {
  name           = "FoodProducts"
  billing_mode   = "PROVISIONED"
  hash_key       = "product_name"
  read_capacity  = 1
  write_capacity = 1

  lifecycle {
    ignore_changes = [tags]
  }

  attribute {
    name = "product_name"
    type = "S"
  }

  attribute {
    name = "special"
    type = "N"
  }

  global_secondary_index {
    name            = "special_GSI"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1

    key_schema {
      attribute_name = "special"
      key_type       = "HASH"
    }
  }
}

resource "aws_dynamodb_table_item" "products" {
  for_each = local.products

  table_name = aws_dynamodb_table.food_products.name
  hash_key   = aws_dynamodb_table.food_products.hash_key

  item = jsonencode(merge(
    {
      product_name   = { S = each.value.product_name_str }
      product_id     = { S = each.value.product_id_str }
      price_in_cents = { N = tostring(each.value.price_in_cents_int) }
      description    = { S = each.value.description_str }
      tags           = { L = [for tag in each.value.tag_str_arr : { S = tag }] }
    },
    try(each.value.special_int, null) == null ? {} : {
      special = { N = tostring(each.value.special_int) }
    }
  ))
}
