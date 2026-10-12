mock_provider "aws" {}

run "lab_final_state" {
  command = plan

  assert {
    condition = (
      aws_dynamodb_table.food_products.name == "FoodProducts" &&
      aws_dynamodb_table.food_products.hash_key == "product_name" &&
      aws_dynamodb_table.food_products.billing_mode == "PROVISIONED" &&
      aws_dynamodb_table.food_products.read_capacity == 1 &&
      aws_dynamodb_table.food_products.write_capacity == 1
    )
    error_message = "The table must match the lab name, primary key and provisioned capacity."
  }

  assert {
    condition = (
      one(aws_dynamodb_table.food_products.global_secondary_index).name == "special_GSI" &&
      one(one(aws_dynamodb_table.food_products.global_secondary_index).key_schema).attribute_name == "special" &&
      one(one(aws_dynamodb_table.food_products.global_secondary_index).key_schema).key_type == "HASH" &&
      one(aws_dynamodb_table.food_products.global_secondary_index).projection_type == "ALL" &&
      one(aws_dynamodb_table.food_products.global_secondary_index).read_capacity == 1 &&
      one(aws_dynamodb_table.food_products.global_secondary_index).write_capacity == 1
    )
    error_message = "The special_GSI index must match the lab schema and capacity."
  }

  assert {
    condition = (
      length(aws_dynamodb_table_item.products) == 26 &&
      jsondecode(aws_dynamodb_table_item.products["apple pie"].item).price_in_cents.N == "4595" &&
      jsondecode(aws_dynamodb_table_item.products["apple pie"].item).special.N == "1" &&
      length([for product in local.products : product if try(product.special_int, null) != null]) == 6
    )
    error_message = "The table must contain all 26 original products with numeric prices and six specials."
  }

  assert {
    condition = (
      !can(jsondecode(aws_dynamodb_table_item.products["blueberry bagel"].item).special) &&
      contains([for tag in jsondecode(aws_dynamodb_table_item.products["blueberry bagel"].item).tags.L : tag.S], "out of stock")
    )
    error_message = "Non-special items must omit special and retain their original availability tags."
  }
}
